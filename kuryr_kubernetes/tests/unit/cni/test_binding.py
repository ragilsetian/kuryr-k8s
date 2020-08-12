# Copyright 2017 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import os
from unittest import mock
import uuid


from os_vif import objects as osv_objects
from os_vif.objects import fields as osv_fields
from oslo_config import cfg
from oslo_utils import uuidutils

from kuryr_kubernetes.cni.binding import base
from kuryr_kubernetes.cni.binding import sriov
from kuryr_kubernetes.cni.binding import vhostuser
from kuryr_kubernetes import constants as k_const
from kuryr_kubernetes import exceptions
from kuryr_kubernetes import objects
from kuryr_kubernetes.tests import base as test_base
from kuryr_kubernetes.tests import fake

CONF = cfg.CONF


class TestDriverMixin(test_base.TestCase):
    def setUp(self):
        super(TestDriverMixin, self).setUp()
        self.instance_info = osv_objects.instance_info.InstanceInfo(
            uuid=uuid.uuid4(), name='foo')
        self.ifname = 'c_interface'
        self.netns = '/proc/netns/1234'

        # Mock IPDB context managers
        self.ipdbs = {}
        self.m_bridge_iface = mock.Mock(__exit__=mock.Mock(return_value=None))
        self.m_c_iface = mock.Mock()
        self.m_h_iface = mock.Mock()
        self.h_ipdb, self.h_ipdb_exit = self._mock_ipdb_context_manager(None)
        self.c_ipdb, self.c_ipdb_exit = self._mock_ipdb_context_manager(
            self.netns)
        self.m_create = mock.Mock()
        self.h_ipdb.create = mock.Mock(
            return_value=mock.Mock(
                __enter__=mock.Mock(return_value=self.m_create),
                __exit__=mock.Mock(return_value=None)))
        self.c_ipdb.create = mock.Mock(
            return_value=mock.Mock(
                __enter__=mock.Mock(return_value=self.m_create),
                __exit__=mock.Mock(return_value=None)))

    def _mock_ipdb_context_manager(self, netns):
        mock_ipdb = mock.Mock(
            interfaces={
                'bridge': mock.Mock(
                    __enter__=mock.Mock(return_value=self.m_bridge_iface),
                    __exit__=mock.Mock(return_value=None),
                    mtu=1,
                ),
                'c_interface': mock.Mock(
                    __enter__=mock.Mock(return_value=self.m_c_iface),
                    __exit__=mock.Mock(return_value=None),
                ),
                'h_interface': mock.Mock(
                    __enter__=mock.Mock(return_value=self.m_h_iface),
                    __exit__=mock.Mock(return_value=None),
                ),
            }
        )
        mock_exit = mock.Mock(return_value=None)
        mock_ipdb.__exit__ = mock_exit
        mock_ipdb.__enter__ = mock.Mock(return_value=mock_ipdb)
        self.ipdbs[netns] = mock_ipdb

        return mock_ipdb, mock_exit

    @mock.patch('kuryr_kubernetes.cni.binding.base._need_configure_l3')
    @mock.patch('kuryr_kubernetes.cni.binding.base.get_ipdb')
    @mock.patch('os_vif.plug')
    def _test_connect(self, m_vif_plug, m_get_ipdb, m_need_l3, report=None):
        def get_ipdb(netns=None):
            return self.ipdbs[netns]

        m_get_ipdb.side_effect = get_ipdb
        m_need_l3.return_value = True

        base.connect(self.vif, self.instance_info, self.ifname, self.netns,
                     report)
        m_vif_plug.assert_called_once_with(self.vif, self.instance_info)
        self.m_c_iface.add_ip.assert_called_once_with('192.168.0.2/24')
        if report:
            report.assert_called_once()

    @mock.patch('kuryr_kubernetes.cni.binding.base.get_ipdb')
    @mock.patch('os_vif.unplug')
    def _test_disconnect(self, m_vif_unplug, m_get_ipdb, report=None):
        def get_ipdb(netns=None):
            return self.ipdbs[netns]
        m_get_ipdb.side_effect = get_ipdb

        base.disconnect(self.vif, self.instance_info, self.ifname, self.netns,
                        report)
        m_vif_unplug.assert_called_once_with(self.vif, self.instance_info)
        if report:
            report.assert_called_once()


class TestOpenVSwitchDriver(TestDriverMixin, test_base.TestCase):
    def setUp(self):
        super(TestOpenVSwitchDriver, self).setUp()
        self.vif = fake._fake_vif(osv_objects.vif.VIFOpenVSwitch)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.report_drivers_health')
    @mock.patch('os.getpid', mock.Mock(return_value=123))
    @mock.patch('kuryr_kubernetes.linux_net_utils.create_ovs_vif_port')
    def test_connect(self, mock_create_ovs, m_report):
        self._test_connect(report=m_report)
        self.assertEqual(3, self.h_ipdb_exit.call_count)
        self.assertEqual(2, self.c_ipdb_exit.call_count)
        self.c_ipdb.create.assert_called_once_with(
            ifname=self.ifname, peer='h_interface', kind='veth')
        self.assertEqual(1, self.m_create.mtu)
        self.assertEqual(str(self.vif.address),
                         self.m_create.address)
        self.m_create.up.assert_called_once_with()
        self.assertEqual(123, self.m_h_iface.net_ns_pid)
        self.assertEqual(1, self.m_h_iface.mtu)
        self.m_h_iface.up.assert_called_once_with()

        mock_create_ovs.assert_called_once_with(
            'bridge', 'h_interface', '89eccd45-43e9-43d8-b4cc-4c13db13f782',
            '3e:94:b7:31:a0:83', 'kuryr')

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.report_drivers_health')
    @mock.patch('kuryr_kubernetes.linux_net_utils.delete_ovs_vif_port')
    def test_disconnect(self, mock_delete_ovs, m_report):
        self._test_disconnect(report=m_report)
        mock_delete_ovs.assert_called_once_with('bridge', 'h_interface')


class TestBridgeDriver(TestDriverMixin, test_base.TestCase):
    def setUp(self):
        super(TestBridgeDriver, self).setUp()
        self.vif = fake._fake_vif(osv_objects.vif.VIFBridge)

    @mock.patch('os.getpid', mock.Mock(return_value=123))
    def test_connect(self):
        self._test_connect()

        self.m_h_iface.remove.assert_called_once_with()

        self.assertEqual(3, self.h_ipdb_exit.call_count)
        self.assertEqual(2, self.c_ipdb_exit.call_count)
        self.c_ipdb.create.assert_called_once_with(
            ifname=self.ifname, peer='h_interface', kind='veth')
        self.assertEqual(1, self.m_create.mtu)
        self.assertEqual(str(self.vif.address),
                         self.m_create.address)
        self.m_create.up.assert_called_once_with()
        self.assertEqual(123, self.m_h_iface.net_ns_pid)
        self.assertEqual(1, self.m_h_iface.mtu)
        self.m_h_iface.up.assert_called_once_with()

        self.m_bridge_iface.add_port.assert_called_once_with('h_interface')

    def test_disconnect(self):
        self._test_disconnect()


class TestNestedVlanDriver(TestDriverMixin, test_base.TestCase):
    def setUp(self):
        super(TestNestedVlanDriver, self).setUp()
        self.vif = fake._fake_vif(objects.vif.VIFVlanNested)
        self.vif.vlan_id = 7
        CONF.set_override('link_iface', 'bridge', group='binding')
        self.addCleanup(CONF.clear_override, 'link_iface', group='binding')

    def test_connect(self):
        self._test_connect()

        self.assertEqual(2, self.h_ipdb_exit.call_count)
        self.assertEqual(3, self.c_ipdb_exit.call_count)

        self.assertEqual(self.ifname, self.m_h_iface.ifname)
        self.assertEqual(1, self.m_h_iface.mtu)
        self.assertEqual(str(self.vif.address), self.m_h_iface.address)
        self.m_h_iface.up.assert_called_once_with()

    def test_connect_mtu_mismatch(self):
        self.vif.network.mtu = 2
        self.assertRaises(exceptions.CNIBindingFailure, self._test_connect)

    def test_disconnect(self):
        self._test_disconnect()


class TestNestedMacvlanDriver(TestDriverMixin, test_base.TestCase):
    def setUp(self):
        super(TestNestedMacvlanDriver, self).setUp()
        self.vif = fake._fake_vif(objects.vif.VIFMacvlanNested)
        CONF.set_override('link_iface', 'bridge', group='binding')
        self.addCleanup(CONF.clear_override, 'link_iface', group='binding')

    def test_connect(self):
        self._test_connect()

        self.assertEqual(2, self.h_ipdb_exit.call_count)
        self.assertEqual(3, self.c_ipdb_exit.call_count)

        self.assertEqual(self.ifname, self.m_h_iface.ifname)
        self.assertEqual(1, self.m_h_iface.mtu)
        self.assertEqual(str(self.vif.address), self.m_h_iface.address)
        self.m_h_iface.up.assert_called_once_with()

    def test_connect_mtu_mismatch(self):
        self.vif.network.mtu = 2
        self.assertRaises(exceptions.CNIBindingFailure, self._test_connect)

    def test_disconnect(self):
        self._test_disconnect()


class TestSriovDriver(TestDriverMixin, test_base.TestCase):
    def setUp(self):
        super(TestSriovDriver, self).setUp()
        self.vif = fake._fake_vif(objects.vif.VIFSriov)
        self.vif.physnet = 'physnet2'
        self.pci_info = mock.MagicMock()
        self.vif.pod_link = 'pod_link'
        self.vif.pod_name = 'pod_1'
        self.pci = mock.Mock()

        self.device_ids = ['pci_dev_1']
        self.device = mock.Mock()
        self.device.device_ids = self.device_ids
        self.device.resource_name = 'intel.com/sriov'

        self.cont_devs = [self.device]
        self.container = mock.Mock()
        self.container.devices = self.cont_devs

        self.pod_containers = [self.container]
        self.pod_resource = mock.Mock()
        self.pod_resource.containers = self.pod_containers
        self.pod_resource.name = 'pod_1'

        self.resources = [self.pod_resource]

        CONF.set_override('physnet_resource_mappings', 'physnet2:sriov',
                          group='sriov')
        self.addCleanup(CONF.clear_override, 'physnet_resource_mappings',
                        group='sriov')
        CONF.set_override('device_plugin_resource_prefix', 'intel.com',
                          group='sriov')
        CONF.set_override('resource_driver_mappings', 'sriov:igbvf',
                          group='sriov')
        CONF.set_override('enable_node_annotations', True,
                          group='sriov')

    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_save_pci_info')
    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_process_vif')
    def test_connect(self, m_proc_vif, m_save_pci):
        m_proc_vif.return_value = self.pci_info

        self._test_connect()
        m_save_pci.assert_called_once_with(self.vif.id, self.pci_info)

    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_save_pci_info')
    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_process_vif')
    def test_connect_no_annotations(self, m_proc_vif, m_save_pci):
        m_proc_vif.return_value = self.pci_info
        CONF.set_override('enable_node_annotations', False,
                          group='sriov')

        self._test_connect()
        m_save_pci.assert_not_called()

    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_return_device_driver')
    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_remove_pci_info')
    def test_disconnect(self, m_remove_pci, m_return_device):
        m_remove_pci.return_value = None
        m_return_device.return_value = None
        self._test_disconnect()
        m_remove_pci.assert_called_once_with(self.vif.id)

    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_return_device_driver')
    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_remove_pci_info')
    def test_disconnect_no_annotations(self, m_remove_pci, m_return_device):
        m_return_device.return_value = None
        m_remove_pci.return_value = None
        CONF.set_override('enable_node_annotations', False,
                          group='sriov')
        self._test_disconnect()
        m_remove_pci.assert_not_called()

    @mock.patch('kuryr_kubernetes.clients.get_pod_resources_client')
    @mock.patch('kuryr_kubernetes.cni.binding.sriov.VIFSriovDriver.'
                '_get_resource_by_physnet')
    def test_process_vif(self, m_get_res_ph, m_get_prc):
        cls = sriov.VIFSriovDriver
        m_driver = mock.Mock(spec=cls)

        m_driver._make_resource.return_value = 'intel.com/sriov'
        m_driver._get_pod_devices.return_value = ['pci_dev_2']
        m_driver._get_driver_by_res.return_value = 'igbvf'
        m_driver._compute_pci.return_value = self.pci_info

        pod_resources_list = mock.Mock()
        pod_resources_list.pod_resources = self.resources
        pod_resources_client = mock.Mock()
        pod_resources_client.list.return_value = pod_resources_list
        m_get_prc.return_value = pod_resources_client

        self.assertEqual(self.pci_info, cls._process_vif(m_driver, self.vif,
                                                         self.ifname,
                                                         self.netns))
        m_driver._compute_pci.assert_called_once_with('pci_dev_1', 'igbvf',
                                                      self.vif.pod_link,
                                                      self.vif, self.ifname,
                                                      self.netns)

    def test_get_resource_by_physnet(self):
        cls = sriov.VIFSriovDriver
        m_driver = mock.Mock(spec=cls)
        self.assertEqual(
            'sriov', cls._get_resource_by_physnet(m_driver, self.vif.physnet))

    def test_make_resource(self):
        cls = sriov.VIFSriovDriver
        m_driver = mock.Mock(spec=cls)
        self.assertEqual('intel.com/sriov', cls._make_resource(m_driver,
                                                               'sriov'))

    def test_get_driver_by_res(self):
        cls = sriov.VIFSriovDriver
        m_driver = mock.Mock(spec=cls)
        self.assertEqual('igbvf', cls._get_driver_by_res(m_driver, 'sriov'))

    def test_compute_pci_vfio(self):
        cls = sriov.VIFSriovDriver
        m_driver = mock.Mock(spec=cls)
        CONF.set_override('resource_driver_mappings', 'sriov:vfio-pci',
                          group='sriov')
        vf_name = 'enp3s0s1'
        vf_index = '1'
        pf = 'enp1s0f1'
        new_driver = 'vfio-pci'
        old_driver = 'igbvf'
        m_driver._get_vf_info.return_value = (vf_name, vf_index, pf,
                                              self.pci_info)
        m_driver._bind_device.return_value = old_driver

        self.assertEqual(self.pci_info, cls._compute_pci(m_driver, self.pci,
                                                         new_driver,
                                                         self.vif.pod_link,
                                                         self.vif,
                                                         self.ifname,
                                                         self.netns))
        m_driver._get_vf_info.assert_called_once_with(self.pci, new_driver)
        m_driver._set_vf_mac.assert_called_once_with(pf, vf_index,
                                                     self.vif.address)
        m_driver._bind_device.assert_called_once_with(self.pci, new_driver)
        m_driver._annotate_device.assert_called_once_with(self.vif.pod_link,
                                                          self.pci, old_driver,
                                                          new_driver,
                                                          self.vif.id)

    def test_compute_pci_netdevice(self):
        cls = sriov.VIFSriovDriver
        m_driver = mock.Mock(spec=cls)
        CONF.set_override('resource_driver_mappings', 'sriov:igbvf',
                          group='sriov')
        vf_name = 'enp3s0s1'
        vf_index = '1'
        pf = 'enp1s0f1'
        new_driver = 'igbvf'
        m_driver._get_vf_info.return_value = (vf_name, vf_index, pf,
                                              self.pci_info)
        m_driver._move_to_netns.return_value = self.pci_info

        self.assertEqual(self.pci_info, cls._compute_pci(m_driver, self.pci,
                                                         new_driver,
                                                         self.vif.pod_link,
                                                         self.vif,
                                                         self.ifname,
                                                         self.netns))

        m_driver._get_vf_info.assert_called_once_with(self.pci, new_driver)
        m_driver._move_to_netns.assert_called_once_with(self.pci, self.ifname,
                                                        self.netns, self.vif,
                                                        vf_name, vf_index, pf,
                                                        self.pci_info)
        m_driver._annotate_device.assert_called_once_with(self.vif.pod_link,
                                                          self.pci, new_driver,
                                                          new_driver,
                                                          self.vif.id)

    def test_return_device_driver(self):
        cls = sriov.VIFSriovDriver
        m_driver = mock.Mock(spec=cls)
        self.vif.id = 'id'
        old_driver = 'igbvf'
        new_driver = 'vfio-pci'
        pci = 'pci_dev_1'
        m_driver._get_pod_devices.return_value = {
            pci: {
                k_const.K8S_ANNOTATION_NEUTRON_PORT: 'id',
                k_const.K8S_ANNOTATION_OLD_DRIVER: old_driver,
                k_const.K8S_ANNOTATION_CURRENT_DRIVER: new_driver
            }
        }

        cls._return_device_driver(m_driver, self.vif)
        m_driver._bind_device.assert_called_once_with(pci, old_driver,
                                                      new_driver)


class TestVHostUserDriver(TestDriverMixin, test_base.TestCase):
    def setUp(self):
        super(TestVHostUserDriver, self).setUp()
        self.vu_mount_point = '/var/run/cni'
        self.vu_ovs_path = '/var/run/openvswitch'
        CONF.set_override('mount_point', self.vu_mount_point,
                          group='vhostuser')
        CONF.set_override('ovs_vhu_path', self.vu_ovs_path,
                          group='vhostuser')
        self.vif = fake._fake_vif(osv_objects.vif.VIFVHostUser)
        self.vif.path = self.vu_mount_point
        self.vif.address = '64:0f:2b:5f:0c:1c'
        self.port_name = vhostuser._get_vhostuser_port_name(self.vif)
        self.cont_id = uuidutils.generate_uuid()

    @mock.patch('kuryr_kubernetes.cni.binding.base._need_configure_l3')
    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.report_drivers_health')
    @mock.patch('os.rename')
    @mock.patch('os.path.exists', mock.Mock(return_value=True))
    @mock.patch('kuryr_kubernetes.cni.binding.vhostuser.VIFVHostUserDriver.'
                '_write_config')
    @mock.patch('kuryr_kubernetes.cni.binding.vhostuser._check_sock_file')
    @mock.patch('os_vif.plug')
    def test_connect_client(self, m_vif_plug, m_check_sock, m_write_conf,
                            m_os_rename, m_report, m_need_l3):
        m_need_l3.return_value = False
        self.vif.mode = osv_fields.VIFVHostUserMode.CLIENT
        m_check_sock.return_value = True
        base.connect(self.vif, self.instance_info, self.ifname, self.netns,
                     m_report, container_id=self.cont_id)
        vu_dst_socket = os.path.join(self.vu_mount_point, self.port_name)
        vu_src_socket = os.path.join(self.vu_ovs_path, self.port_name)

        m_vif_plug.assert_called_once_with(self.vif, self.instance_info)
        m_os_rename.assert_called_once_with(vu_src_socket, vu_dst_socket)
        m_write_conf.assert_called_once_with(self.cont_id, self.ifname,
                                             self.port_name, self.vif)
        m_report.assert_called_once()

    @mock.patch('kuryr_kubernetes.cni.binding.base._need_configure_l3')
    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.report_drivers_health')
    @mock.patch('kuryr_kubernetes.cni.binding.vhostuser.VIFVHostUserDriver.'
                '_write_config')
    @mock.patch('os_vif.plug')
    def test_connect_server(self, m_vif_plug, m_write_conf,
                            m_report, m_need_l3):
        m_need_l3.return_value = False
        self.vif.mode = osv_fields.VIFVHostUserMode.SERVER
        base.connect(self.vif, self.instance_info, self.ifname, self.netns,
                     m_report, container_id=self.cont_id)
        m_vif_plug.assert_called_once_with(self.vif, self.instance_info)
        m_write_conf.assert_called_once_with(self.cont_id, self.ifname,
                                             self.port_name, self.vif)
        m_report.assert_called_once()

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.report_drivers_health')
    @mock.patch('kuryr_kubernetes.cni.binding.vhostuser._check_sock_file',
                mock.Mock(return_value=False))
    @mock.patch('kuryr_kubernetes.cni.binding.vhostuser.VIFVHostUserDriver.'
                '_write_config', mock.Mock())
    @mock.patch('os_vif.plug')
    def test_connect_nosocket(self, m_vif_plug, m_report):
        self.vif.mode = osv_fields.VIFVHostUserMode.CLIENT
        self.assertRaises(exceptions.CNIError, base.connect, self.vif,
                          self.instance_info, self.ifname, self.netns,
                          m_report, container_id=self.cont_id)

    @mock.patch('kuryr_kubernetes.cni.plugins.k8s_cni_registry.'
                'K8sCNIRegistryPlugin.report_drivers_health')
    @mock.patch('kuryr_kubernetes.cni.binding.vhostuser._get_vhu_sock')
    @mock.patch('os.remove')
    @mock.patch('os.path.exists', mock.Mock(return_value=True))
    @mock.patch('os_vif.unplug')
    def test_disconnect(self, m_os_unplug, m_os_remove, m_get_vhu_sock,
                        m_report):
        m_get_vhu_sock.return_value = self.port_name
        base.disconnect(self.vif, self.instance_info, self.ifname, self.netns,
                        m_report, container_id=self.cont_id)
        conf_file_path = '{}/{}-{}'.format(self.vu_mount_point,
                                           self.cont_id, self.ifname)
        vhu_sock_path = '{}/{}'.format(self.vu_mount_point,
                                       self.port_name)
        os_remove_calls = [mock.call(vhu_sock_path), mock.call(conf_file_path)]
        m_os_remove.assert_has_calls(os_remove_calls)
