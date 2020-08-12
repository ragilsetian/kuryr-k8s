# Copyright 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest import mock

from oslo_serialization import jsonutils

from kuryr_kubernetes.cni.daemon import service
from kuryr_kubernetes.cni.plugins import k8s_cni_registry
from kuryr_kubernetes import exceptions
from kuryr_kubernetes.tests import base
from kuryr_kubernetes.tests import fake


class TestDaemonServer(base.TestCase):
    def setUp(self):
        super(TestDaemonServer, self).setUp()
        healthy = mock.Mock()
        self.plugin = k8s_cni_registry.K8sCNIRegistryPlugin({}, healthy)
        self.health_registry = mock.Mock()
        self.srv = service.DaemonServer(self.plugin, self.health_registry)

        self.srv.application.testing = True
        self.test_client = self.srv.application.test_client()
        params = {'config_kuryr': {}, 'CNI_ARGS': 'foo=bar',
                  'CNI_CONTAINERID': 'baz', 'CNI_COMMAND': 'ADD'}
        self.params_str = jsonutils.dumps(params)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.add')
    def test_add(self, m_add):
        vif = fake._fake_vif()
        m_add.return_value = vif

        resp = self.test_client.post('/addNetwork', data=self.params_str,
                                     content_type='application/json')

        m_add.assert_called_once_with(mock.ANY)
        self.assertEqual(
            fake._fake_vif_string(vif.obj_to_primitive()).encode(), resp.data)
        self.assertEqual(202, resp.status_code)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.add')
    def test_add_timeout(self, m_add):
        m_add.side_effect = exceptions.ResourceNotReady(mock.Mock())

        resp = self.test_client.post('/addNetwork', data=self.params_str,
                                     content_type='application/json')

        m_add.assert_called_once_with(mock.ANY)
        self.assertEqual(504, resp.status_code)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.add')
    def test_add_error(self, m_add):
        m_add.side_effect = Exception

        resp = self.test_client.post('/addNetwork', data=self.params_str,
                                     content_type='application/json')

        m_add.assert_called_once_with(mock.ANY)
        self.assertEqual(500, resp.status_code)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.delete')
    def test_delete(self, m_delete):
        resp = self.test_client.post('/delNetwork', data=self.params_str,
                                     content_type='application/json')

        m_delete.assert_called_once_with(mock.ANY)
        self.assertEqual(204, resp.status_code)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.delete')
    def test_delete_timeout(self, m_delete):
        m_delete.side_effect = exceptions.ResourceNotReady(mock.Mock())
        resp = self.test_client.post('/delNetwork', data=self.params_str,
                                     content_type='application/json')

        m_delete.assert_called_once_with(mock.ANY)
        self.assertEqual(204, resp.status_code)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.delete')
    def test_delete_error(self, m_delete):
        m_delete.side_effect = Exception
        resp = self.test_client.post('/delNetwork', data=self.params_str,
                                     content_type='application/json')

        m_delete.assert_called_once_with(mock.ANY)
        self.assertEqual(500, resp.status_code)


class TestCNIDaemonWatcherService(base.TestCase):
    def setUp(self):
        super(TestCNIDaemonWatcherService, self).setUp()
        self.registry = {}
        self.pod = {'metadata': {'namespace': 'testing',
                                 'name': 'default'},
                    'vif_unplugged': False,
                    'del_receieved': False}
        self.healthy = mock.Mock()
        self.watcher = service.CNIDaemonWatcherService(
            0, self.registry, self.healthy)

    @mock.patch('oslo_concurrency.lockutils.lock')
    def test_on_deleted(self, m_lock):
        pod = self.pod
        pod['vif_unplugged'] = True
        pod_name = 'testing/default'
        self.registry[pod_name] = pod
        self.watcher.on_deleted(pod)
        self.assertNotIn(pod_name, self.registry)

    @mock.patch('oslo_concurrency.lockutils.lock')
    def test_on_deleted_false(self, m_lock):
        pod = self.pod
        pod_name = 'testing/default'
        self.registry[pod_name] = pod
        self.watcher.on_deleted(pod)
        self.assertIn(pod_name, self.registry)
        self.assertIs(True, pod['del_received'])
