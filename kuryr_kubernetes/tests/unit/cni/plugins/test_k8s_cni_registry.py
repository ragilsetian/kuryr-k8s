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

from oslo_config import cfg

from kuryr_kubernetes.cni.plugins import k8s_cni_registry
from kuryr_kubernetes import exceptions
from kuryr_kubernetes.tests import base
from kuryr_kubernetes.tests import fake


class TestK8sCNIRegistryPlugin(base.TestCase):
    def setUp(self):
        super(TestK8sCNIRegistryPlugin, self).setUp()
        self.default_iface = 'baz'
        self.additional_iface = 'eth1'
        self.pod = {'metadata': {'name': 'foo', 'uid': 'bar',
                                 'namespace': 'default'}}
        self.vifs = fake._fake_vifs_dict()
        registry = {'default/foo': {'pod': self.pod, 'vifs': self.vifs,
                                    'containerid': None,
                                    'vif_unplugged': False,
                                    'del_received': False}}
        healthy = mock.Mock()
        self.plugin = k8s_cni_registry.K8sCNIRegistryPlugin(registry, healthy)
        self.params = mock.Mock(args=mock.Mock(K8S_POD_NAME='foo',
                                               K8S_POD_NAMESPACE='default'),
                                CNI_IFNAME=self.default_iface, CNI_NETNS=123,
                                CNI_CONTAINERID='cont_id')

    @mock.patch('oslo_concurrency.lockutils.lock')
    @mock.patch('kuryr_kubernetes.cni.binding.base.connect')
    def test_add_present(self, m_connect, m_lock):
        self.plugin.add(self.params)

        m_lock.assert_called_with('default/foo', external=True)
        m_connect.assert_any_call(mock.ANY, mock.ANY, self.default_iface,
                                  123, report_health=mock.ANY,
                                  is_default_gateway=True,
                                  container_id='cont_id')
        m_connect.assert_any_call(mock.ANY, mock.ANY, self.additional_iface,
                                  123, report_health=mock.ANY,
                                  is_default_gateway=False,
                                  container_id='cont_id')
        self.assertEqual('cont_id',
                         self.plugin.registry['default/foo']['containerid'])

    @mock.patch('oslo_concurrency.lockutils.lock')
    @mock.patch('kuryr_kubernetes.cni.binding.base.disconnect')
    def test_del_present(self, m_disconnect, m_lock):
        self.plugin.delete(self.params)

        m_lock.assert_called_with('default/foo', external=True)
        m_disconnect.assert_any_call(mock.ANY, mock.ANY, self.default_iface,
                                     123, report_health=mock.ANY,
                                     is_default_gateway=True,
                                     container_id='cont_id')
        m_disconnect.assert_any_call(mock.ANY, mock.ANY, self.additional_iface,
                                     123, report_health=mock.ANY,
                                     is_default_gateway=False,
                                     container_id='cont_id')
        self.assertIn('default/foo', self.plugin.registry)
        self.assertEqual(True,
                         self.plugin.registry['default/foo']['vif_unplugged'])

    @mock.patch('oslo_concurrency.lockutils.lock')
    @mock.patch('kuryr_kubernetes.cni.binding.base.disconnect')
    def test_remove_pod_from_registry_after_del(self, m_disconnect, m_lock):
        self.plugin.registry['default/foo']['del_received'] = True
        self.plugin.delete(self.params)

        m_lock.assert_called_with('default/foo', external=True)
        self.assertNotIn('default/foo', self.plugin.registry)
        m_disconnect.assert_any_call(mock.ANY, mock.ANY, self.default_iface,
                                     123, report_health=mock.ANY,
                                     is_default_gateway=True,
                                     container_id='cont_id')
        m_disconnect.assert_any_call(mock.ANY, mock.ANY, self.additional_iface,
                                     123, report_health=mock.ANY,
                                     is_default_gateway=False,
                                     container_id='cont_id')

    @mock.patch('kuryr_kubernetes.cni.binding.base.disconnect')
    def test_del_wrong_container_id(self, m_disconnect):
        registry = {'default/foo': {'pod': self.pod, 'vifs': self.vifs,
                                    'containerid': 'different'}}
        healthy = mock.Mock()
        self.plugin = k8s_cni_registry.K8sCNIRegistryPlugin(registry, healthy)
        self.plugin.delete(self.params)

        m_disconnect.assert_not_called()

    @mock.patch('oslo_concurrency.lockutils.lock')
    @mock.patch('time.sleep', mock.Mock())
    @mock.patch('kuryr_kubernetes.cni.binding.base.connect')
    def test_add_present_on_5_try(self, m_connect, m_lock):
        se = [KeyError] * 5
        se.append({'pod': self.pod, 'vifs': self.vifs, 'containerid': None,
                   'vif_unplugged': False, 'del_received': False})
        se.append({'pod': self.pod, 'vifs': self.vifs, 'containerid': None,
                   'vif_unplugged': False, 'del_received': False})
        se.append({'pod': self.pod, 'vifs': self.vifs, 'containerid': None,
                   'vif_unplugged': False, 'del_received': False})
        m_getitem = mock.Mock(side_effect=se)
        m_setitem = mock.Mock()
        m_registry = mock.Mock(__getitem__=m_getitem, __setitem__=m_setitem)
        self.plugin.registry = m_registry
        self.plugin.add(self.params)

        m_lock.assert_called_with('default/foo', external=True)
        m_setitem.assert_called_once_with('default/foo',
                                          {'pod': self.pod,
                                           'vifs': self.vifs,
                                           'containerid': 'cont_id',
                                           'vif_unplugged': False,
                                           'del_received': False})
        m_connect.assert_any_call(mock.ANY, mock.ANY, self.default_iface,
                                  123, report_health=mock.ANY,
                                  is_default_gateway=True,
                                  container_id='cont_id')
        m_connect.assert_any_call(mock.ANY, mock.ANY, self.additional_iface,
                                  123, report_health=mock.ANY,
                                  is_default_gateway=False,
                                  container_id='cont_id')

    @mock.patch('time.sleep', mock.Mock())
    def test_add_not_present(self):
        cfg.CONF.set_override('vif_annotation_timeout', 0, group='cni_daemon')
        self.addCleanup(cfg.CONF.set_override, 'vif_annotation_timeout', 120,
                        group='cni_daemon')

        m_getitem = mock.Mock(side_effect=KeyError)
        m_registry = mock.Mock(__getitem__=m_getitem)
        self.plugin.registry = m_registry
        self.assertRaises(exceptions.ResourceNotReady, self.plugin.add,
                          self.params)
