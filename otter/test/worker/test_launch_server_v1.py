"""
Unittests for the launch_server_v1 launch config.
"""
import mock
import json

from twisted.trial.unittest import TestCase
from twisted.internet.defer import Deferred, fail, succeed
from twisted.internet.task import Clock
from twisted.python.failure import Failure

from otter.worker.launch_server_v1 import (
    private_ip_addresses,
    endpoints,
    add_to_load_balancer,
    add_to_load_balancers,
    server_details,
    wait_for_active,
    create_server,
    launch_server,
    prepare_launch_config,
    delete_server,
    remove_from_load_balancer,
    public_endpoint_url,
    UnexpectedServerStatus,
    ServerDeleted,
    verified_delete,
    LB_MAX_RETRIES, LB_RETRY_INTERVAL_RANGE
)


from otter.test.utils import mock_log, patch, CheckFailure, mock_treq, matches
from testtools.matchers import IsInstance, StartsWith
from otter.util.http import APIError, RequestError, wrap_request_error
from otter.util.config import set_config_data
from otter.util.deferredutils import unwrap_first_error, TimedOutError

from otter.test.utils import iMock
from otter.undo import IUndoStack

from otter.rest.bobby import set_bobby
from otter.bobby import BobbyClient

fake_config = {
    'regionOverrides': {},
    'cloudServersOpenStack': 'cloudServersOpenStack',
    'cloudLoadBalancers': 'cloudLoadBalancers'
}

fake_service_catalog = [
    {'type': 'compute',
     'name': 'cloudServersOpenStack',
     'endpoints': [
         {'region': 'DFW', 'publicURL': 'http://dfw.openstack/'},
         {'region': 'ORD', 'publicURL': 'http://ord.openstack/'}
     ]},
    {'type': 'lb',
     'name': 'cloudLoadBalancers',
     'endpoints': [
         {'region': 'DFW', 'publicURL': 'http://dfw.lbaas/'},
     ]}
]


class UtilityTests(TestCase):
    """
    Tests for non-specific utilities that should be refactored out of the
    worker implementation eventually.
    """

    def test_private_ip_addresses(self):
        """
        private_ip_addresses returns all private IPv4 addresses from a
        complete server body.
        """
        addresses = {
            'private': [
                {'addr': '10.0.0.1', 'version': 4},
                {'addr': '10.0.0.2', 'version': 4},
                {'addr': '::1', 'version': 6}
            ],
            'public': [
                {'addr': '50.50.50.50', 'version': 4},
                {'addr': '::::', 'version': 6}
            ]}

        result = private_ip_addresses({'server': {'addresses': addresses}})
        self.assertEqual(result, ['10.0.0.1', '10.0.0.2'])

    def test_endpoints(self):
        """
        endpoints will return only the named endpoint in a specific region.
        """
        self.assertEqual(
            sorted(endpoints(fake_service_catalog,
                             'cloudServersOpenStack',
                             'DFW')),
            [{'region': 'DFW', 'publicURL': 'http://dfw.openstack/'}])

    def test_public_endpoint_url(self):
        """
        public_endpoint_url returns the first publicURL for the named service
        in a specific region.
        """
        self.assertEqual(
            public_endpoint_url(fake_service_catalog, 'cloudServersOpenStack',
                                'DFW'),
            'http://dfw.openstack/')


expected_headers = {
    'content-type': ['application/json'],
    'accept': ['application/json'],
    'x-auth-token': ['my-auth-token'],
    'User-Agent': ['OtterScale/0.0']
}


error_body = '{"code": 500, "message": "Internal Server Error"}'


class LoadBalancersTests(TestCase):
    """
    Test adding to one or more load balancers.
    """
    def setUp(self):
        """
        set up test dependencies for load balancers.
        """
        self.json_content = {'nodes': [{'id': 1}]}
        self.treq = patch(self, 'otter.worker.launch_server_v1.treq',
                          new=mock_treq(code=200, json_content=self.json_content,
                                        method='post'))
        patch(self, 'otter.util.http.treq', new=self.treq)
        self.log = mock_log()
        self.log.msg.return_value = None

        self.undo = iMock(IUndoStack)

        self.max_retries = 12
        set_config_data({'worker': {'lb_max_retries': self.max_retries,
                                    'lb_retry_interval_range': [5, 7]}})
        self.addCleanup(set_config_data, {})

        # patch random_interval
        self.retry_interval = 6
        self.rand_interval = patch(self, 'otter.worker.launch_server_v1.random_interval')
        self.rand_interval.return_value = self.interval_func = mock.Mock(
            return_value=self.retry_interval)

    def test_add_to_load_balancer(self):
        """
        add_to_load_balancer will make a properly formed post request to
        the specified load balancer endpoint witht he specified auth token,
        load balancer id, port, and ip address.
        """
        d = add_to_load_balancer(self.log, 'http://url/', 'my-auth-token',
                                 {'loadBalancerId': 12345,
                                  'port': 80},
                                 '192.168.1.1',
                                 self.undo)

        result = self.successResultOf(d)
        self.assertEqual(result, self.json_content)

        self.treq.post.assert_called_once_with(
            'http://url/loadbalancers/12345/nodes',
            headers=expected_headers,
            data=mock.ANY,
            log=matches(IsInstance(self.log.__class__))
        )

        data = self.treq.post.mock_calls[0][2]['data']

        self.assertEqual(json.loads(data),
                         {'nodes': [{'address': '192.168.1.1',
                                     'port': 80,
                                     'condition': 'ENABLED',
                                     'type': 'PRIMARY'}]})

        self.treq.json_content.assert_called_once_with(mock.ANY)

    def test_add_lb_retries(self):
        """
        add_to_load_balancer will retry again until it succeeds
        """
        self.codes = [422] * 10 + [200]
        self.treq.post.side_effect = lambda *_, **ka: succeed(mock.Mock(code=self.codes.pop(0)))
        clock = Clock()

        d = add_to_load_balancer(self.log, 'http://url/', 'my-auth-token',
                                 {'loadBalancerId': 12345,
                                  'port': 80},
                                 '192.168.1.1',
                                 self.undo, clock=clock)
        clock.pump([self.retry_interval] * 11)
        result = self.successResultOf(d)
        self.assertEqual(result, self.json_content)
        self.assertEqual(self.treq.post.mock_calls,
                         [mock.call('http://url/loadbalancers/12345/nodes',
                                    headers=expected_headers, data=mock.ANY,
                                    log=matches(IsInstance(self.log.__class__)))] * 11)
        self.rand_interval.assert_called_once_with(5, 7)

    def test_add_lb_defaults_retries_configs(self):
        """
        add_to_load_balancer will use defaults LB_RETRY_INTERVAL_RANGE, LB_MAX_RETRIES
        when not configured
        """
        set_config_data({})
        self.treq.post.side_effect = lambda *a, **kw: succeed(mock.Mock(code=422))
        clock = Clock()
        d = add_to_load_balancer(self.log, 'http://url/', 'my-auth-token',
                                 {'loadBalancerId': 12345,
                                  'port': 80},
                                 '192.168.1.1',
                                 self.undo, clock=clock)
        clock.pump([self.retry_interval] * LB_MAX_RETRIES)
        self.failureResultOf(d, RequestError)
        self.assertEqual(self.treq.post.mock_calls,
                         [mock.call('http://url/loadbalancers/12345/nodes',
                                    headers=expected_headers, data=mock.ANY,
                                    log=matches(IsInstance(self.log.__class__)))]
                         * (LB_MAX_RETRIES + 1))
        self.rand_interval.assert_called_once_with(*LB_RETRY_INTERVAL_RANGE)

    def failed_add_to_lb(self, code=500):
        """
        Helper function to ensure add_to_load_balancer fails by returning failure
        again and again until it times out
        """
        self.treq.post.side_effect = lambda *a, **kw: succeed(mock.Mock(code=code))
        clock = Clock()
        d = add_to_load_balancer(self.log, 'http://url/', 'my-auth-token',
                                 {'loadBalancerId': 12345,
                                  'port': 80},
                                 '192.168.1.1',
                                 self.undo, clock=clock)
        clock.pump([self.retry_interval] * self.max_retries)
        return d

    def test_addl_b_retries_times_out(self):
        """
        add_to_load_balancer will retry again and again for worker.lb_max_retries times.
        It will fail after that. This also checks that API failure is propogated
        """
        d = self.failed_add_to_lb(422)

        f = self.failureResultOf(d, RequestError)
        self.assertEqual(f.value.reason.value.code, 422)
        self.assertEqual(
            self.treq.post.mock_calls,
            [mock.call('http://url/loadbalancers/12345/nodes',
                       headers=expected_headers, data=mock.ANY,
                       log=matches(IsInstance(self.log.__class__)))] * (self.max_retries + 1))

    def test_add_lb_retries_logs(self):
        """
        add_to_load_balancer will log all failures while it is trying
        """
        self.codes = [500, 503, 422, 422, 401, 200]
        bad_codes_len = len(self.codes) - 1
        self.treq.post.side_effect = lambda *_, **ka: succeed(mock.Mock(code=self.codes.pop(0)))
        clock = Clock()

        d = add_to_load_balancer(self.log, 'http://url/', 'my-auth-token',
                                 {'loadBalancerId': 12345,
                                  'port': 80},
                                 '192.168.1.1',
                                 self.undo, clock=clock)
        clock.pump([self.retry_interval] * 6)
        self.successResultOf(d)
        self.log.msg.assert_has_calls(
            [mock.call('Got LB error while {m}: {e}', loadbalancer_id=12345,
                       m='add_node', e=matches(IsInstance(RequestError)))] * bad_codes_len)

    def test_add_lb_retries_logs_unexpected_errors(self):
        """
        add_to_load_balancer will log unexpeted failures while it is trying
        """
        self.codes = [500, 503, 422, 422, 401, 200]
        bad_codes = [500, 503, 401]
        self.treq.post.side_effect = lambda *_, **ka: succeed(mock.Mock(code=self.codes.pop(0)))
        clock = Clock()

        d = add_to_load_balancer(self.log, 'http://url/', 'my-auth-token',
                                 {'loadBalancerId': 12345,
                                  'port': 80},
                                 '192.168.1.1',
                                 self.undo, clock=clock)
        clock.pump([self.retry_interval] * 6)
        self.successResultOf(d)
        self.log.msg.assert_has_calls(
            [mock.call('Unexpected status {status} while {msg}: {error}',
                       status=code, msg='add_node',
                       error=matches(IsInstance(RequestError)), loadbalancer_id=12345)
             for code in bad_codes])

    test_add_lb_retries_logs_unexpected_errors.skip = 'Lets log all errors for now'

    def test_add_to_load_balancer_pushes_remove_onto_undo_stack(self):
        """
        add_to_load_balancer pushes an inverse remove_from_load_balancer
        operation onto the undo stack.
        """
        d = add_to_load_balancer(self.log, 'http://url/', 'my-auth-token',
                                 {'loadBalancerId': 12345,
                                  'port': 80},
                                 '192.168.1.1',
                                 self.undo)

        self.successResultOf(d)
        self.undo.push.assert_called_once_with(
            remove_from_load_balancer, matches(IsInstance(self.log.__class__)),
            'http://url/', 'my-auth-token',
            12345,
            1)

    def test_add_to_load_balancer_doesnt_push_onto_undo_stack_on_failure(self):
        """
        add_to_load_balancer doesn't push an operation onto the undo stack
        if it fails.
        """
        d = self.failed_add_to_lb()
        self.failureResultOf(d, RequestError)
        self.assertFalse(self.undo.push.called)

    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancer')
    def test_add_to_load_balancers(self, add_to_load_balancer):
        """
        Add to load balancers will call add_to_load_balancer multiple times and
        for each load balancer configuration and return all of the results.
        """
        d1 = Deferred()
        d2 = Deferred()
        add_to_load_balancer_deferreds = [d1, d2]

        def _add_to_load_balancer(
                log, endpoint, auth_token, lb_config, ip_address, undo):
            return add_to_load_balancer_deferreds.pop(0)

        add_to_load_balancer.side_effect = _add_to_load_balancer

        d = add_to_load_balancers(self.log, 'http://url/', 'my-auth-token',
                                  [{'loadBalancerId': 12345,
                                    'port': 80},
                                   {'loadBalancerId': 54321,
                                    'port': 81}],
                                  '192.168.1.1',
                                  self.undo)

        # Include the ID and port in the response so that we can verify
        # that add_to_load_balancers associates the response with the correct
        # load balancer.

        d2.callback((54321, 81))
        d1.callback((12345, 80))

        results = self.successResultOf(d)

        self.assertEqual(sorted(results), [(12345, (12345, 80)),
                                           (54321, (54321, 81))])

    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancer')
    def test_add_to_load_balancers_is_serial(self, add_to_load_balancer):
        """
        add_to_load_balancers calls add_to_load_balancer in series.
        """
        d1 = Deferred()
        d2 = Deferred()

        add_to_load_balancer_deferreds = [d1, d2]

        def _add_to_load_balancer(*args, **kwargs):
            return add_to_load_balancer_deferreds.pop(0)

        add_to_load_balancer.side_effect = _add_to_load_balancer

        d = add_to_load_balancers(self.log, 'http://url/', 'my-auth-token',
                                  [{'loadBalancerId': 12345,
                                    'port': 80},
                                   {'loadBalancerId': 54321,
                                    'port': 81}],
                                  '192.168.1.1',
                                  self.undo)

        self.assertNoResult(d)

        add_to_load_balancer.assert_called_once_with(
            self.log,
            'http://url/',
            'my-auth-token',
            {'loadBalancerId': 12345, 'port': 80},
            '192.168.1.1',
            self.undo
        )

        d1.callback(None)

        add_to_load_balancer.assert_called_with(
            self.log,
            'http://url/',
            'my-auth-token',
            {'loadBalancerId': 54321, 'port': 81},
            '192.168.1.1',
            self.undo
        )

        d2.callback(None)

        self.successResultOf(d)

    def test_add_to_load_balancers_no_lb_configs(self):
        """
        add_to_load_balancers returns a Deferred that fires with an empty list
        when no load balancers are configured.
        """

        d = add_to_load_balancers(self.log, 'http://url/', 'my-auth-token',
                                  [],
                                  '192.168.1.1',
                                  self.undo)

        self.assertEqual(self.successResultOf(d), [])

    def test_remove_from_load_balancer(self):
        """
        remove_from_load_balancer makes a DELETE request against the
        URL represting the load balancer node.
        """
        self.treq.delete.return_value = succeed(mock.Mock(code=200))
        self.treq.content.return_value = succeed('')

        d = remove_from_load_balancer(self.log, 'http://url/', 'my-auth-token', 12345, 1)

        self.assertEqual(self.successResultOf(d), None)
        self.treq.delete.assert_called_once_with(
            'http://url/loadbalancers/12345/nodes/1',
            headers=expected_headers, log=matches(IsInstance(self.log.__class__)))

    def test_remove_from_load_balancer_on_404(self):
        """
        remove_from_load_balancer makes a DELETE request against the
        URL represting the load balancer node and ignores if it is already deleted
        i.e. it returns 404. It also logs it
        """
        self.treq.delete.return_value = succeed(mock.Mock(code=404))
        self.treq.content.return_value = succeed(json.dumps({'message': 'LB does not exist'}))

        d = remove_from_load_balancer(self.log, 'http://url/', 'my-auth-token', 12345, 1)

        self.assertEqual(self.successResultOf(d), None)
        self.log.msg.assert_any_call(
            'Node to delete does not exist', loadbalancer_id=12345, node_id=1)

    def test_remove_from_load_balancer_on_422_LB_deleted(self):
        """
        remove_from_load_balancer makes a DELETE request against the
        URL represting the load balancer node and ignores if the load balancer
        has been deleted and is considered immutable (a 422 response with a
        particular message). It also logs it
        """
        message = "The load balancer is deleted and considered immutable."
        body = {"message": message, "code": 422}
        mock_treq(code=422, content=json.dumps(body), method='delete', treq_mock=self.treq)

        d = remove_from_load_balancer(self.log, 'http://url/', 'my-auth-token', 12345, 1)

        self.assertEqual(self.successResultOf(d), None)
        self.log.msg.assert_any_call(message, loadbalancer_id=12345, node_id=1)

    def test_remove_from_load_balancer_on_422_Pending_delete(self):
        """
        remove_from_load_balancer makes a DELETE request against the
        URL represting the load balancer node and ignores if the load balancer
        is in PENDING_DELETE and is considered immutable (a 422 response with a
        particular message). It also logs it
        """
        message = ("Load Balancer '12345' has a status of 'PENDING_DELETE' and "
                   "is considered immutable.")
        body = {"message": message, "code": 422}
        mock_treq(code=422, content=json.dumps(body), method='delete', treq_mock=self.treq)

        d = remove_from_load_balancer(self.log, 'http://url/', 'my-auth-token', 12345, 1)

        self.assertEqual(self.successResultOf(d), None)
        self.log.msg.assert_any_call(message, loadbalancer_id=12345, node_id=1)

    def test_remove_from_load_balancer_fails_on_422_LB_other(self):
        """
        remove_from_load_balancer makes a DELETE request against the
        URL represting the load balancer node and will fail if the 422 response
        is not a result of the LB being deleted.
        """
        body = {
            "message": ("Load Balancer '1' has a status of 'ERROR' and is "
                        "considered immutable."),
            "code": 422
        }
        mock_treq(code=422, content=json.dumps(body), method='delete', treq_mock=self.treq)

        d = remove_from_load_balancer(self.log, 'http://url/', 'my-auth-token', 12345, 1)

        self.failureResultOf(d, RequestError)
        self.log.msg.assert_any_call(
            'Got LB error while {m}: {e}', m='remove_node', e=mock.ANY,
            loadbalancer_id=12345, node_id=1)

    test_remove_from_load_balancer_fails_on_422_LB_other.skip = 'Until we bail out early on ERROR'

    def test_removelb_retries(self):
        """
        remove_from_load_balancer will retry again until it succeeds and retry interval
        will be random number based on lb_retry_interval_range config value
        """
        self.codes = [422] * 7 + [500] * 3 + [200]
        self.treq.delete.side_effect = lambda *_, **ka: succeed(mock.Mock(code=self.codes.pop(0)))
        self.treq.content.side_effect = lambda *a, **ka: succeed(
            json.dumps({'message': 'PENDING_UPDATE'}))
        clock = Clock()

        d = remove_from_load_balancer(
            self.log, 'http://url/', 'my-auth-token', 12345, 1, clock=clock)

        clock.pump([self.retry_interval] * 11)
        self.assertIsNone(self.successResultOf(d))
        # delete calls made?
        self.assertEqual(self.treq.delete.mock_calls,
                         [mock.call('http://url/loadbalancers/12345/nodes/1',
                                    headers=expected_headers,
                                    log=matches(IsInstance(self.log.__class__)))] * 11)
        # Expected logs?
        self.assertEqual(self.log.msg.mock_calls[0],
                         mock.call('Removing from load balancer',
                                   loadbalancer_id=12345, node_id=1))
        self.assertEqual(
            self.log.msg.mock_calls[1:-1],
            [mock.call('Got LB error while {m}: {e}', m='remove_node',
                       e=matches(IsInstance(RequestError)),
                       loadbalancer_id=12345, node_id=1)] * 10)
        self.assertEqual(self.log.msg.mock_calls[-1],
                         mock.call('Removed from load balancer',
                                   loadbalancer_id=12345, node_id=1))
        # Random interval from config
        self.rand_interval.assert_called_once_with(5, 7)
        self.interval_func.assert_has_calls([mock.call(CheckFailure(RequestError))] * 10)

    def test_removelb_limits_retries(self):
        """
        remove_from_load_balancer will retry again and again for LB_MAX_RETRIES times.
        It will fail after that
        """
        self.treq.delete.side_effect = lambda *_, **ka: succeed(mock.Mock(code=422))
        self.treq.content.side_effect = lambda *a, **ka: succeed(
            json.dumps({'message': 'PENDING_UPDATE'}))
        clock = Clock()

        d = remove_from_load_balancer(
            self.log, 'http://url/', 'my-auth-token', 12345, 1, clock=clock)

        clock.pump([self.retry_interval] * self.max_retries)
        # failed?
        failure = self.failureResultOf(d, RequestError)
        self.assertEqual(failure.value.reason.value.code, 422)
        # delete calls made?
        self.assertEqual(
            self.treq.delete.mock_calls,
            [mock.call('http://url/loadbalancers/12345/nodes/1',
                       headers=expected_headers,
                       log=matches(IsInstance(self.log.__class__)))] * (self.max_retries + 1))
        # Expected logs?
        self.assertEqual(self.log.msg.mock_calls[0],
                         mock.call('Removing from load balancer',
                                   loadbalancer_id=12345, node_id=1))
        self.assertEqual(
            self.log.msg.mock_calls[1:],
            [mock.call('Got LB error while {m}: {e}', m='remove_node',
                       e=matches(IsInstance(RequestError)),
                       loadbalancer_id=12345, node_id=1)] * (self.max_retries + 1))
        # Interval func call max times?
        self.rand_interval.assert_called_once_with(5, 7)
        self.interval_func.assert_has_calls(
            [mock.call(CheckFailure(RequestError))] * self.max_retries)

    def test_removelb_retries_uses_defaults(self):
        """
        remove_from_load_balancer will retry based on default config if lb_max_retries
        or lb_retry_interval_range is not found
        """
        set_config_data({})
        self.treq.delete.side_effect = lambda *_, **ka: succeed(mock.Mock(code=422))
        self.treq.content.side_effect = lambda *a, **ka: succeed(
            json.dumps({'message': 'PENDING_UPDATE'}))
        clock = Clock()

        d = remove_from_load_balancer(
            self.log, 'http://url/', 'my-auth-token', 12345, 1, clock=clock)

        clock.pump([self.retry_interval] * LB_MAX_RETRIES)
        # failed?
        failure = self.failureResultOf(d, RequestError)
        self.assertEqual(failure.value.reason.value.code, 422)
        # delete calls made?
        self.assertEqual(
            self.treq.delete.mock_calls,
            [mock.call('http://url/loadbalancers/12345/nodes/1',
                       headers=expected_headers,
                       log=matches(IsInstance(self.log.__class__)))] * (LB_MAX_RETRIES + 1))
        # Expected logs?
        self.assertEqual(self.log.msg.mock_calls[0],
                         mock.call('Removing from load balancer',
                                   loadbalancer_id=12345, node_id=1))
        self.assertEqual(
            self.log.msg.mock_calls[1:],
            [mock.call('Got LB error while {m}: {e}', m='remove_node',
                       e=matches(IsInstance(RequestError)),
                       loadbalancer_id=12345, node_id=1)] * (LB_MAX_RETRIES + 1))
        # Interval func call max times?
        self.rand_interval.assert_called_once_with(*LB_RETRY_INTERVAL_RANGE)
        self.interval_func.assert_has_calls(
            [mock.call(CheckFailure(RequestError))] * LB_MAX_RETRIES)

    def test_removelb_retries_logs_unexpected_errors(self):
        """
        add_to_load_balancer will log unexpeted failures while it is trying
        """
        self.codes = [500, 503, 422, 422, 401, 200]
        bad_codes = [500, 503, 401]
        self.treq.delete.side_effect = lambda *_, **ka: succeed(mock.Mock(code=self.codes.pop(0)))
        clock = Clock()

        d = remove_from_load_balancer(
            self.log, 'http://url/', 'my-auth-token', 12345, 1, clock=clock)

        clock.pump([self.retry_interval] * 6)
        self.successResultOf(d)
        self.log.msg.assert_has_calls(
            [mock.call('Unexpected status {status} while {msg}: {error}',
                       status=code, msg='remove_node',
                       error=matches(IsInstance(RequestError)), loadbalancer_id=12345,
                       node_id=1)
             for code in bad_codes])

    test_removelb_retries_logs_unexpected_errors.skip = 'Lets log all errors for now'


class BobbyServerTests(TestCase):
    """
    Test server manipulation functions with Bobby present -- will become part of the
    regular tests later on.
    """
    def setUp(self):
        """
        Set up test dependencies.
        """
        self.log = mock_log()
        set_config_data(fake_config)
        self.addCleanup(set_config_data, {})

        self.treq = patch(self, 'otter.worker.launch_server_v1.treq')
        patch(self, 'otter.util.http.treq', new=self.treq)

        self.generate_server_name = patch(
            self,
            'otter.worker.launch_server_v1.generate_server_name')
        self.generate_server_name.return_value = 'as000000'

        self.scaling_group_uuid = '1111111-11111-11111-11111111'

        self.scaling_group = mock.Mock(uuid=self.scaling_group_uuid, tenant_id='1234')

        self.undo = iMock(IUndoStack)

        set_bobby(BobbyClient("http://127.0.0.1:9876/"))

    def tearDown(self):
        """
        Reset bobby dependencies.
        """
        set_bobby(None)

    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancers')
    @mock.patch('otter.worker.launch_server_v1.create_server')
    @mock.patch('otter.worker.launch_server_v1.wait_for_active')
    @mock.patch('otter.bobby.BobbyClient.create_server', return_value=succeed(''))
    def test_launch_server_bobby(self, bobby_create_server, wait_for_active, create_server,
                                 add_to_load_balancers):
        """
        launch_server creates a server, waits until the server is active then
        adds the server's first private IPv4 address to any load balancers.
        """
        launch_config = {'server': {'imageRef': '1', 'flavorRef': '1'},
                         'loadBalancers': [
                             {'loadBalancerId': 12345, 'port': 80},
                             {'loadBalancerId': 54321, 'port': 81}
                         ]}

        server_detail = {
            'server': {
                'id': '1',
                'addresses': {'private': [
                    {'version': 4, 'addr': '10.0.0.1'}]}}}

        create_server.return_value = succeed(server_detail)

        wait_for_active.return_value = succeed(server_detail)

        add_to_load_balancers.return_value = succeed([
            (12345, ('10.0.0.1', 80)),
            (54321, ('10.0.0.1', 81))
        ])

        d = launch_server(self.log,
                          'DFW',
                          self.scaling_group,
                          fake_service_catalog,
                          'my-auth-token',
                          launch_config,
                          self.undo)

        result = self.successResultOf(d)
        self.assertEqual(
            result,
            (server_detail, [
                (12345, ('10.0.0.1', 80)),
                (54321, ('10.0.0.1', 81))]))

        bobby_create_server.assert_called_once_with('1234', self.scaling_group_uuid, '1')


class ServerTests(TestCase):
    """
    Test server manipulation functions.
    """
    def setUp(self):
        """
        Set up test dependencies.
        """
        self.log = mock_log()
        set_config_data(fake_config)
        self.addCleanup(set_config_data, {})

        self.treq = patch(self, 'otter.worker.launch_server_v1.treq')
        patch(self, 'otter.util.http.treq', new=self.treq)

        self.generate_server_name = patch(
            self,
            'otter.worker.launch_server_v1.generate_server_name')
        self.generate_server_name.return_value = 'as000000'

        self.scaling_group_uuid = '1111111-11111-11111-11111111'

        self.scaling_group = mock.Mock(uuid=self.scaling_group_uuid, tenant_id='1234')

        self.undo = iMock(IUndoStack)

    def test_server_details(self):
        """
        server_details will perform a properly formed GET request against
        the server endpoint and return the decoded json content.
        """
        response = mock.Mock()
        response.code = 200

        self.treq.get.return_value = succeed(response)

        d = server_details('http://url/', 'my-auth-token', 'serverId')

        results = self.successResultOf(d)

        self.assertEqual(results, self.treq.json_content.return_value)

    def test_server_details_on_404(self):
        """
        server_details will raise a :class:`ServerDeleted` error when it
        it gets a 404 back in the response
        """
        mock_treq(code=404, content='not found', method='get',
                  treq_mock=self.treq)

        d = server_details('http://url/', 'my-auth-token', 'serverId')
        self.failureResultOf(d, ServerDeleted)

    def test_server_details_propagates_api_failure(self):
        """
        server_details will propagate API failures.
        """
        response = mock.Mock()
        response.code = 500

        self.treq.get.return_value = succeed(response)
        self.treq.content.return_value = succeed(error_body)

        d = server_details('http://url/', 'my-auth-token', 'serverId')

        failure = self.failureResultOf(d)
        self.assertTrue(failure.check(RequestError))
        real_failure = failure.value.reason

        self.assertTrue(real_failure.check(APIError))
        self.assertEqual(real_failure.value.code, 500)

    def test_create_server(self):
        """
        create_server will perform a properly formed POST request to the
        server endpoint and return the decoded json content.
        """
        response = mock.Mock()
        response.code = 202

        self.treq.post.return_value = succeed(response)

        server_config = {
            'name': 'someServer',
            'imageRef': '1',
            'flavorRef': '3'
        }

        d = create_server('http://url/', 'my-auth-token', server_config)

        result = self.successResultOf(d)

        self.assertEqual(result, self.treq.json_content.return_value)

    def test_create_server_limits(self):
        """
        create_server when called many times will post only 2 requests at a time
        """
        deferreds = [Deferred() for i in range(3)]
        post_ds = deferreds[:]
        self.treq.post.side_effect = lambda *a, **kw: deferreds.pop(0)

        server_config = {
            'name': 'someServer',
            'imageRef': '1',
            'flavorRef': '3'
        }

        ret_ds = [create_server('http://url/', 'my-auth-token', server_config)
                  for i in range(3)]

        # no result in any of them and only first 2 treq.post is called
        [self.assertNoResult(d) for d in ret_ds]
        self.assertTrue(self.treq.post.call_count, 2)

        # fire one deferred and notice that 3rd treq.post is called
        post_ds[0].callback(mock.Mock(code=202))
        self.assertTrue(self.treq.post.call_count, 3)
        self.successResultOf(ret_ds[0])

        # fire others
        post_ds[1].callback(mock.Mock(code=202))
        post_ds[2].callback(mock.Mock(code=202))
        self.successResultOf(ret_ds[1])
        self.successResultOf(ret_ds[2])

    def test_create_server_propagates_api_failure(self):
        """
        create_server will propagate API failures.
        """
        response = mock.Mock()
        response.code = 500

        self.treq.post.return_value = succeed(response)
        self.treq.content.return_value = succeed(error_body)

        d = create_server('http://url/', 'my-auth-token', {})

        failure = self.failureResultOf(d)
        self.assertTrue(failure.check(RequestError))
        real_failure = failure.value.reason

        self.assertTrue(real_failure.check(APIError))
        self.assertEqual(real_failure.value.code, 500)

    @mock.patch('otter.worker.launch_server_v1.server_details')
    def test_wait_for_active(self, server_details):
        """
        wait_for_active will poll server_details until the status transitions
        to our expected status at which point it will return the complete
        server_details.
        """
        clock = Clock()

        server_status = ['BUILD']

        def _server_status(*args, **kwargs):
            return succeed({'server': {'status': server_status[0]}})

        server_details.side_effect = _server_status

        d = wait_for_active(self.log,
                            'http://url/', 'my-auth-token', 'serverId',
                            interval=5, clock=clock)

        server_details.assert_called_with('http://url/', 'my-auth-token',
                                          'serverId', log=mock.ANY)
        self.assertEqual(server_details.call_count, 1)

        server_status[0] = 'ACTIVE'

        clock.advance(5)

        server_details.assert_called_with('http://url/', 'my-auth-token',
                                          'serverId', log=mock.ANY)
        self.assertEqual(server_details.call_count, 2)

        result = self.successResultOf(d)

        self.assertEqual(result['server']['status'], server_status[0])

    @mock.patch('otter.worker.launch_server_v1.server_details')
    def test_wait_for_active_errors(self, server_details):
        """
        wait_for_active will errback it's Deferred if it encounters a non-active
        state transition.
        """
        clock = Clock()

        server_status = ['BUILD', 'ERROR']

        def _server_status(*args, **kwargs):
            return succeed({'server': {'status': server_status.pop(0)}})

        server_details.side_effect = _server_status

        d = wait_for_active(self.log,
                            'http://url/', 'my-auth-token', 'serverId',
                            interval=5, clock=clock)

        clock.advance(5)

        failure = self.failureResultOf(d)
        self.assertTrue(failure.check(UnexpectedServerStatus))

        self.assertEqual(failure.value.server_id, 'serverId')
        self.assertEqual(failure.value.status, 'ERROR')
        self.assertEqual(failure.value.expected_status, 'ACTIVE')

    @mock.patch('otter.worker.launch_server_v1.server_details')
    def test_wait_for_active_continues_looping_on_500(self, server_details):
        """
        wait_for_active will keep looping if ``server_details`` raises other
        exceptions, for instance RequestErrors.
        """
        clock = Clock()

        server_details.return_value = fail(
            RequestError(Failure(APIError(500, '', {})), 'url'))

        d = wait_for_active(self.log,
                            'http://url/', 'my-auth-token', 'serverId',
                            interval=5, clock=clock)

        self.assertNoResult(d)
        server_details.return_value = succeed({'server': {'status': 'ACTIVE'}})

        clock.advance(5)

        result = self.successResultOf(d)
        self.assertEqual(result['server']['status'], 'ACTIVE')

    @mock.patch('otter.worker.launch_server_v1.server_details')
    def test_wait_for_active_stops_looping_on_server_deletion(self, server_details):
        """
        wait_for_active will errback it's Deferred if ``server_details`` raises
        a ``ServerDeletion`` error
        """
        clock = Clock()

        server_details.return_value = fail(ServerDeleted('1234'))
        d = wait_for_active(self.log,
                            'http://url/', 'my-auth-token', 'serverId',
                            interval=5, clock=clock)

        failure = self.failureResultOf(d)
        self.assertTrue(failure.check(ServerDeleted))

    @mock.patch('otter.worker.launch_server_v1.server_details')
    def test_wait_for_active_stops_looping_on_error(self, server_details):
        """
        wait_for_active stops looping when it encounters an error.
        """
        clock = Clock()
        server_status = ['BUILD', 'ERROR']

        def _server_status(*args, **kwargs):
            return succeed({'server': {'status': server_status.pop(0)}})

        server_details.side_effect = _server_status

        d = wait_for_active(self.log,
                            'http://url/', 'my-auth-token', 'serverId',
                            interval=5, clock=clock)

        # This gets called once immediately then every 5 seconds.
        self.assertEqual(server_details.call_count, 1)

        clock.advance(5)

        self.assertEqual(server_details.call_count, 2)

        clock.advance(5)

        # This has not been called a 3rd time because we encountered an error,
        # and the looping call stopped.
        self.assertEqual(server_details.call_count, 2)

        self.failureResultOf(d)

    @mock.patch('otter.worker.launch_server_v1.server_details')
    def test_wait_for_active_stops_looping_on_success(self, server_details):
        """
        wait_for_active stops looping when it encounters the active state.
        """
        clock = Clock()
        server_status = ['BUILD', 'ACTIVE']

        def _server_status(*args, **kwargs):
            return succeed({'server': {'status': server_status.pop(0)}})

        server_details.side_effect = _server_status

        d = wait_for_active(self.log,
                            'http://url/', 'my-auth-token', 'serverId',
                            interval=5, clock=clock)

        # This gets called once immediately then every 5 seconds.
        self.assertEqual(server_details.call_count, 1)

        clock.advance(5)

        self.assertEqual(server_details.call_count, 2)

        clock.advance(5)

        # This has not been called a 3rd time because we encountered the active
        # state and the looping call stopped.
        self.assertEqual(server_details.call_count, 2)

        self.successResultOf(d)

    @mock.patch('otter.worker.launch_server_v1.server_details')
    def test_wait_for_active_stops_looping_on_timeout(self, server_details):
        """
        wait_for_active stops looping when the timeout passes
        """
        clock = Clock()
        server_details.side_effect = lambda *args, **kwargs: succeed(
            {'server': {'status': 'BUILD'}})

        d = wait_for_active(self.log,
                            'http://url/', 'my-auth-token', 'serverId',
                            interval=5, timeout=6, clock=clock)

        # This gets called once immediately then every 5 seconds.
        self.assertEqual(server_details.call_count, 1)
        clock.advance(5)
        self.assertEqual(server_details.call_count, 2)
        self.assertNoResult(d)

        clock.advance(1)
        self.failureResultOf(d, TimedOutError)

        # the loop has stopped
        clock.advance(5)
        self.assertEqual(server_details.call_count, 2)

    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancers')
    @mock.patch('otter.worker.launch_server_v1.create_server')
    @mock.patch('otter.worker.launch_server_v1.wait_for_active')
    def test_launch_server(self, wait_for_active, create_server,
                           add_to_load_balancers):
        """
        launch_server creates a server, waits until the server is active then
        adds the server's first private IPv4 address to any load balancers.
        """
        launch_config = {'server': {'imageRef': '1', 'flavorRef': '1'},
                         'loadBalancers': [
                             {'loadBalancerId': 12345, 'port': 80},
                             {'loadBalancerId': 54321, 'port': 81}
                         ]}

        load_balancer_metadata = {
            'rax:auto_scaling_server_name': 'as000000',
            'rax:auto_scaling_group_id': '1111111-11111-11111-11111111'}

        prepared_load_balancers = [
            {'loadBalancerId': 12345, 'port': 80,
             'metadata': load_balancer_metadata},
            {'loadBalancerId': 54321, 'port': 81,
             'metadata': load_balancer_metadata}
        ]

        expected_server_config = {
            'imageRef': '1', 'flavorRef': '1', 'name': 'as000000',
            'metadata': {
                'rax:auto_scaling_group_id': '1111111-11111-11111-11111111'}}

        server_details = {
            'server': {
                'id': '1',
                'addresses': {'private': [
                    {'version': 4, 'addr': '10.0.0.1'}]}}}

        create_server.return_value = succeed(server_details)

        wait_for_active.return_value = succeed(server_details)

        add_to_load_balancers.return_value = succeed([
            (12345, ('10.0.0.1', 80)),
            (54321, ('10.0.0.1', 81))
        ])

        log = mock.Mock()
        d = launch_server(log,
                          'DFW',
                          self.scaling_group,
                          fake_service_catalog,
                          'my-auth-token',
                          launch_config,
                          self.undo)

        result = self.successResultOf(d)
        self.assertEqual(
            result,
            (server_details, [
                (12345, ('10.0.0.1', 80)),
                (54321, ('10.0.0.1', 81))]))

        create_server.assert_called_once_with('http://dfw.openstack/',
                                              'my-auth-token',
                                              expected_server_config,
                                              log=mock.ANY)

        wait_for_active.assert_called_once_with(mock.ANY,
                                                'http://dfw.openstack/',
                                                'my-auth-token',
                                                '1')

        log.bind.assert_called_once_with(server_name='as000000')
        log = log.bind.return_value
        log.bind.assert_called_once_with(server_id='1')
        add_to_load_balancers.assert_called_once_with(
            log.bind.return_value, 'http://dfw.lbaas/', 'my-auth-token', prepared_load_balancers,
            '10.0.0.1', self.undo)

    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancers')
    @mock.patch('otter.worker.launch_server_v1.create_server')
    @mock.patch('otter.worker.launch_server_v1.wait_for_active')
    def test_launch_server_propagates_create_server_errors(
            self, wait_for_active, create_server, add_to_load_balancers):
        """
        launch_server will propagate any errors from create_server.
        """
        create_server.return_value = fail(
            APIError(500, "Oh noes")).addErrback(wrap_request_error, 'url')

        d = launch_server(self.log,
                          'DFW',
                          self.scaling_group,
                          fake_service_catalog,
                          'my-auth-token',
                          {'server': {}},
                          self.undo)

        failure = self.failureResultOf(d)
        failure.trap(RequestError)
        real_failure = failure.value.reason

        self.assertTrue(real_failure.check(APIError))
        self.assertEqual(real_failure.value.code, 500)
        self.assertEqual(real_failure.value.body, "Oh noes")

    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancers')
    @mock.patch('otter.worker.launch_server_v1.create_server')
    @mock.patch('otter.worker.launch_server_v1.wait_for_active')
    def test_launch_server_propagates_wait_for_active_errors(
            self, wait_for_active, create_server, add_to_load_balancers):
        """
        launch_server will propagate any errors from wait_for_active.
        """
        launch_config = {'server': {'imageRef': '1', 'flavorRef': '1'},
                         'loadBalancers': []}

        server_details = {
            'server': {
                'id': '1',
                'addresses': {'private': [
                    {'version': 4, 'addr': '10.0.0.1'}]}}}

        create_server.return_value = succeed(server_details)

        wait_for_active.return_value = fail(
            APIError(500, "Oh noes")).addErrback(wrap_request_error, 'url')

        d = launch_server(self.log,
                          'DFW',
                          self.scaling_group,
                          fake_service_catalog,
                          'my-auth-token',
                          launch_config,
                          self.undo)

        failure = self.failureResultOf(d)
        failure.trap(RequestError)
        real_failure = failure.value.reason

        self.assertTrue(real_failure.check(APIError))
        self.assertEqual(real_failure.value.code, 500)
        self.assertEqual(real_failure.value.body, "Oh noes")

    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancers')
    @mock.patch('otter.worker.launch_server_v1.create_server')
    @mock.patch('otter.worker.launch_server_v1.wait_for_active')
    def test_launch_server_propagates_add_to_load_balancers_errors(
            self, wait_for_active, create_server, add_to_load_balancers):
        """
        launch_server will propagate any errors from add_to_load_balancers.
        """
        launch_config = {'server': {'imageRef': '1', 'flavorRef': '1'},
                         'loadBalancers': []}

        server_details = {
            'server': {
                'id': '1',
                'addresses': {'private': [
                    {'version': 4, 'addr': '10.0.0.1'}]}}}

        create_server.return_value = succeed(server_details)

        wait_for_active.return_value = succeed(server_details)

        add_to_load_balancers.return_value = fail(
            APIError(500, "Oh noes")).addErrback(wrap_request_error, 'url')

        d = launch_server(self.log,
                          'DFW',
                          self.scaling_group,
                          fake_service_catalog,
                          'my-auth-token',
                          launch_config,
                          self.undo)

        failure = self.failureResultOf(d)
        failure.trap(RequestError)
        real_failure = failure.value.reason

        self.assertTrue(real_failure.check(APIError))
        self.assertEqual(real_failure.value.code, 500)
        self.assertEqual(real_failure.value.body, "Oh noes")

    @mock.patch('otter.worker.launch_server_v1.verified_delete')
    @mock.patch('otter.worker.launch_server_v1.add_to_load_balancers')
    @mock.patch('otter.worker.launch_server_v1.create_server')
    @mock.patch('otter.worker.launch_server_v1.wait_for_active')
    def test_launch_server_pushes_verified_delete_onto_undo(
            self, wait_for_active, create_server, add_to_load_balancers,
            verified_delete):
        """
        launch_server will push verified_delete onto the undo stack
        after the server is successfully created.
        """
        launch_config = {'server': {'imageRef': '1', 'flavorRef': '1'},
                         'loadBalancers': []}

        server_details = {
            'server': {
                'id': '1',
                'addresses': {'private': [
                    {'version': 4, 'addr': '10.0.0.1'}]}}}

        create_server.return_value = Deferred()

        wait_for_active.return_value = succeed(server_details)

        mock_server_response = {'server': {'id': '1',
                                           'addresses': {'private': [{'version': 4,
                                                                      'addr': '10.0.0.1'}]}}}
        mock_lb_response = [(12345, ('10.0.0.1', 80)), (54321, ('10.0.0.1', 81))]
        add_to_load_balancers.return_value = succeed((mock_server_response, mock_lb_response))

        d = launch_server(self.log,
                          'DFW',
                          self.scaling_group,
                          fake_service_catalog,
                          'my-auth-token',
                          launch_config,
                          self.undo)

        # Check that the push hasn't happened because create_server hasn't
        # succeeded yet.
        self.assertEqual(self.undo.push.call_count, 0)

        create_server.return_value.callback(server_details)

        self.successResultOf(d)

        self.undo.push.assert_called_once_with(
            verified_delete,
            mock.ANY,
            'http://dfw.openstack/',
            'my-auth-token',
            '1')

    @mock.patch('otter.worker.launch_server_v1.create_server')
    def test_launch_server_doesnt_push_undo_op_on_create_server_failure(
            self, create_server):
        """
        launch_server won't push anything onto the undo stack if create_server
        fails.
        """
        launch_config = {'server': {'imageRef': '1', 'flavorRef': '1'},
                         'loadBalancers': []}

        create_server.return_value = fail(APIError(500, ''))

        d = launch_server(self.log,
                          'DFW',
                          self.scaling_group,
                          fake_service_catalog,
                          'my-auth-token',
                          launch_config,
                          self.undo)

        self.failureResultOf(d, APIError)

        self.assertEqual(self.undo.push.call_count, 0)


class ConfigPreparationTests(TestCase):
    """
    Test config preparation.
    """
    def setUp(self):
        """
        Configure mocks.
        """
        generate_server_name_patcher = mock.patch(
            'otter.worker.launch_server_v1.generate_server_name')
        self.generate_server_name = generate_server_name_patcher.start()
        self.addCleanup(generate_server_name_patcher.stop)
        self.generate_server_name.return_value = 'as000000'

        self.scaling_group_uuid = '1111111-11111-11111-11111111'

    def test_server_name_suffix(self):
        """
        The server name uses the name specified in the launch config as a
        suffix.
        """
        test_config = {'server': {'name': 'web.example.com'}}
        expected_name = 'web.example.com-as000000'

        launch_config = prepare_launch_config(self.scaling_group_uuid,
                                              test_config)

        self.assertEqual(expected_name, launch_config['server']['name'])

    def test_server_name_no_suffix(self):
        """
        No server name in the launch config means no suffix.
        """
        test_config = {'server': {}}
        expected_name = 'as000000'

        launch_config = prepare_launch_config(self.scaling_group_uuid,
                                              test_config)

        self.assertEqual(expected_name, launch_config['server']['name'])

    def test_server_metadata(self):
        """
        The auto scaling group should be added to the server metadata.
        """
        test_config = {'server': {}}
        expected_metadata = {
            'rax:auto_scaling_group_id': self.scaling_group_uuid}

        launch_config = prepare_launch_config(self.scaling_group_uuid,
                                              test_config)

        self.assertEqual(expected_metadata,
                         launch_config['server']['metadata'])

    def test_server_merge_metadata(self):
        """
        The auto scaling metadata should be merged with specified metadata.
        """
        test_config = {'server': {'metadata': {'foo': 'bar'}}}
        expected_metadata = {
            'rax:auto_scaling_group_id': self.scaling_group_uuid,
            'foo': 'bar'}

        launch_config = prepare_launch_config(self.scaling_group_uuid,
                                              test_config)

        self.assertEqual(expected_metadata,
                         launch_config['server']['metadata'])

    def test_load_balancer_metadata(self):
        """
        auto scaling group and auto scaling server name should be
        added to the node metadata for a load balancer.
        """
        test_config = {'server': {}, 'loadBalancers': [{'id': 1, 'port': 80}]}

        expected_metadata = {
            'rax:auto_scaling_group_id': self.scaling_group_uuid,
            'rax:auto_scaling_server_name': 'as000000'}

        launch_config = prepare_launch_config(self.scaling_group_uuid,
                                              test_config)

        self.assertEqual(expected_metadata,
                         launch_config['loadBalancers'][0]['metadata'])

    def test_load_balancer_metadata_merge(self):
        """
        auto scaling metadata should be merged with user specified metadata.
        """
        test_config = {'server': {}, 'loadBalancers': [
            {'id': 1, 'port': 80, 'metadata': {'foo': 'bar'}}]}

        expected_metadata = {
            'rax:auto_scaling_group_id': self.scaling_group_uuid,
            'rax:auto_scaling_server_name': 'as000000',
            'foo': 'bar'}

        launch_config = prepare_launch_config(self.scaling_group_uuid,
                                              test_config)

        self.assertEqual(expected_metadata,
                         launch_config['loadBalancers'][0]['metadata'])

    def test_launch_config_is_copy(self):
        """
        The input launch config is not mutated by prepare_launch_config.
        """
        test_config = {'server': {}}

        launch_config = prepare_launch_config(self.scaling_group_uuid,
                                              test_config)

        self.assertNotIdentical(test_config, launch_config)


# An instance associated with a single load balancer.
instance_details = (
    'a',
    [(12345, {'nodes': [{'id': 1}]}),
     (54321, {'nodes': [{'id': 2}]})])


class DeleteServerTests(TestCase):
    """
    Test the delete server worker.
    """
    def setUp(self):
        """
        Set up some mocks.
        """
        set_config_data(fake_config)
        self.addCleanup(set_config_data, {})

        self.log = mock_log()
        self.treq = patch(self, 'otter.worker.launch_server_v1.treq')
        patch(self, 'otter.util.http.treq', new=self.treq)

        self.treq.delete.return_value = succeed(mock.Mock(code=404))
        self.treq.content.side_effect = lambda *a, **kw: succeed("")

        self.remove_from_load_balancer = patch(
            self, 'otter.worker.launch_server_v1.remove_from_load_balancer')
        self.remove_from_load_balancer.return_value = succeed(None)

        self.clock = Clock()

    def test_delete_server_deletes_load_balancer_node(self):
        """
        delete_server removes the nodes specified in instance details from
        the associated load balancers.
        """
        d = delete_server(self.log,
                          'DFW',
                          fake_service_catalog,
                          'my-auth-token',
                          instance_details)
        self.successResultOf(d)

        self.remove_from_load_balancer.assert_has_calls([
            mock.call(self.log, 'http://dfw.lbaas/', 'my-auth-token', 12345, 1),
            mock.call(self.log, 'http://dfw.lbaas/', 'my-auth-token', 54321, 2)
        ], any_order=True)

        self.assertEqual(self.remove_from_load_balancer.call_count, 2)

    def test_delete_server(self):
        """
        delete_server performs a DELETE request against the instance URL based
        on the information in instance_details.
        """
        d = delete_server(self.log, 'DFW', fake_service_catalog,
                          'my-auth-token', instance_details)
        self.successResultOf(d)

        self.treq.delete.assert_called_once_with(
            'http://dfw.openstack/servers/a',
            headers=expected_headers, log=mock.ANY)

    def test_delete_server_succeeds_on_unknown_server(self):
        """
        delete_server succeeds and logs if delete calls return 404.
        """
        self.treq.delete.return_value = succeed(mock.Mock(code=404))

        d = delete_server(self.log, 'DFW', fake_service_catalog,
                          'my-auth-token', instance_details)
        self.successResultOf(d)

    def test_delete_server_propagates_loadbalancer_failures(self):
        """
        delete_server propagates any errors from removing server from load
        balancers
        """
        self.remove_from_load_balancer.return_value = fail(
            APIError(500, '')).addErrback(wrap_request_error, 'url')

        d = delete_server(self.log, 'DFW', fake_service_catalog,
                          'my-auth-token', instance_details)
        failure = unwrap_first_error(self.failureResultOf(d))

        self.assertEqual(failure.value.reason.value.code, 500)

    @mock.patch('otter.worker.launch_server_v1.verified_delete')
    def test_delete_server_propagates_verified_delete_failures(self, deleter):
        """
        delete_server fails with an APIError if deleting the server fails.
        """
        deleter.return_value = fail(TimedOutError(3660, 'meh'))

        d = delete_server(self.log, 'DFW', fake_service_catalog,
                          'my-auth-token', instance_details)
        self.failureResultOf(d, TimedOutError)

    def test_verified_delete_retries_until_success(self):
        """
        If the first delete didn't work, wait a bit and try again until the
        server has been deleted, since a server can sit in DELETE state for a
        bit.  Deferred only callbacks when the deletion is done.

        It also logs deletion success.
        """
        self.treq.delete.return_value = succeed(mock.Mock(code=204))
        d = verified_delete(self.log, 'http://url/', 'my-auth-token',
                            'serverId', interval=5, clock=self.clock)
        self.assertEqual(self.treq.delete.call_count, 1)
        self.assertNoResult(d)

        self.treq.delete.return_value = succeed(mock.Mock(code=404))
        self.clock.pump([5])
        self.assertEqual(self.treq.delete.call_count, 2)
        self.successResultOf(d)

        # the loop has stopped
        self.clock.pump([5])
        self.assertEqual(self.treq.delete.call_count, 2)

        # success logged
        self.log.msg.assert_called_with(
            matches(StartsWith("Server deleted successfully")),
            server_id='serverId', time_delete=5)

    def test_verified_delete_retries_verification_until_timeout(self):
        """
        If the deleting fails until the timeout, log a failure and do not
        keep trying to delete.
        """
        self.treq.delete.side_effect = lambda *a, **kw: succeed(mock.Mock(code=500))
        d = verified_delete(self.log, 'http://url/', 'my-auth-token',
                            'serverId', interval=5, timeout=20, clock=self.clock)
        self.assertNoResult(d)

        self.clock.pump([5] * 4)
        self.assertEqual(
            self.treq.delete.mock_calls,
            [mock.call('http://url/servers/serverId', headers=expected_headers,
                       log=mock.ANY)] * 4)
        self.log.err.assert_called_once_with(CheckFailure(TimedOutError),
                                             server_id='serverId')

        # the loop has stopped
        self.clock.pump([5])
        self.assertEqual(self.treq.delete.call_count, 4)
