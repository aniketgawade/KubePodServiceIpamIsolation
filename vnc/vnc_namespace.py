#
# Copyright (c) 2017 Juniper Networks, Inc. All rights reserved.
#

"""
VNC service management for kubernetes
"""

import uuid

from vnc_api.vnc_api import (
    AddressType, IpamSubnets, IdPermsType, IpamSubnetType, NetworkIpam,
    NoIdError, PolicyEntriesType, PolicyRuleType, PortType, Project,
    RefsExistError, SecurityGroup, SubnetType, VirtualNetwork,
    VirtualNetworkType, VnSubnetsType, NetworkPolicy, ActionListType,
    VirtualNetworkPolicyType, SequenceType)
from kube_manager.vnc.config_db import (
    NetworkIpamKM, VirtualNetworkKM, ProjectKM, SecurityGroupKM)
from kube_manager.common.kube_config_db import NamespaceKM
from kube_manager.vnc.vnc_kubernetes_config import (
    VncKubernetesConfig as vnc_kube_config)
from kube_manager.vnc.vnc_common import VncCommon

class VncNamespace(VncCommon):

    def __init__(self, network_policy_mgr):
        self._k8s_event_type = 'Namespace'
        super(VncNamespace, self).__init__(self._k8s_event_type)
        self._name = type(self).__name__
        self._network_policy_mgr = network_policy_mgr
        self._vnc_lib = vnc_kube_config.vnc_lib()
        self._ns_sg = {}
        self._label_cache = vnc_kube_config.label_cache()
        self._logger = vnc_kube_config.logger()
        self._queue = vnc_kube_config.queue()
        ip_fabric_fq_name = vnc_kube_config. \
            cluster_ip_fabric_network_fq_name()
        self._ip_fabric_vn_obj = self._vnc_lib. \
            virtual_network_read(fq_name=ip_fabric_fq_name)
        cluster_service_network_fq_name = vnc_kube_config. \
            cluster_default_service_network_fq_name()
        self._cluster_service_vn_obj = self._vnc_lib. \
            virtual_network_read(fq_name=cluster_service_network_fq_name)

    def _get_namespace(self, ns_name):
        """
        Get namesapce object from cache.
        """
        return NamespaceKM.find_by_name_or_uuid(ns_name)

    def _delete_namespace(self, ns_name):
        """
        Delete namespace object from cache.
        """
        ns = self._get_namespace(ns_name)
        if ns:
            NamespaceKM.delete(ns.uuid)

    def _get_namespace_pod_vn_name(self, ns_name):
        return ns_name + "-pod-network"

    def _get_namespace_service_vn_name(self, ns_name):
        return ns_name + "-service-network"

    def _is_namespace_isolated(self, ns_name):
        """
        Check if this namespace is configured as isolated.
        """
        ns = self._get_namespace(ns_name)
        if ns:
            return ns.is_isolated()

        # Kubernetes namespace obj is not available to check isolation config.
        #
        # Check if the virtual network associated with the namespace is
        # annotated as isolated. If yes, then the namespace is isolated.
        vn_uuid = VirtualNetworkKM.get_ann_fq_name_to_uuid(self, ns_name,
                                                           ns_name)
        if vn_uuid:
            vn_obj = VirtualNetworkKM.get(vn_uuid)
            if vn_obj:
                return vn_obj.is_k8s_namespace_isolated()

        # By default, namespace is not isolated.
        return False

    def _get_network_policy_annotations(self, ns_name):
        ns = self._get_namespace(ns_name)
        if ns:
            return ns.get_network_policy_annotations()
        return None

    def _get_annotated_virtual_network(self, ns_name):
        ns = self._get_namespace(ns_name)
        if ns:
            return ns.get_annotated_network_fq_name()
        return None

    def _set_namespace_pod_virtual_network(self, ns_name, fq_name):
        ns = self._get_namespace(ns_name)
        if ns:
            return ns.set_isolated_pod_network_fq_name(fq_name)
        return None

    def _set_namespace_service_virtual_network(self, ns_name, fq_name):
        ns = self._get_namespace(ns_name)
        if ns:
            return ns.set_isolated_service_network_fq_name(fq_name)
        return None

    def _clear_namespace_label_cache(self, ns_uuid, project):
        if not ns_uuid or \
           ns_uuid not in project.ns_labels:
            return
        ns_labels = project.ns_labels[ns_uuid]
        for label in ns_labels.items() or []:
            key = self._label_cache._get_key(label)
            self._label_cache._remove_label(
                key, self._label_cache.ns_label_cache, label, ns_uuid)
        del project.ns_labels[ns_uuid]

    def _update_namespace_label_cache(self, labels, ns_uuid, project):
        self._clear_namespace_label_cache(ns_uuid, project)
        for label in labels.items():
            key = self._label_cache._get_key(label)
            self._label_cache._locate_label(
                key, self._label_cache.ns_label_cache, label, ns_uuid)
        if labels:
            project.ns_labels[ns_uuid] = labels

    def _create_isolated_ns_virtual_network(self, ns_name, vn_name,
                    proj_obj, ipam_obj=None, provider=None):
        """
        Create a virtual network for this namespace.
        """
        vn = VirtualNetwork(
            name=vn_name, parent_obj=proj_obj,
            virtual_network_properties=VirtualNetworkType(forwarding_mode='l3'),
            address_allocation_mode='flat-subnet-only')

        # Add annotatins on this isolated virtual-network.
        VirtualNetworkKM.add_annotations(self, vn, namespace=ns_name,
                                         name=ns_name, isolated='True')

        try:
            vn_uuid = self._vnc_lib.virtual_network_create(vn)
        except RefsExistError:
            vn_obj = self._vnc_lib.virtual_network_read(
                fq_name=vn.get_fq_name())
            vn_uuid = vn_obj.uuid

        # Instance-Ip for pods on this VN, should be allocated from
        # cluster pod ipam. Attach the cluster pod-ipam object
        # to this virtual network.
        vn.add_network_ipam(ipam_obj, VnSubnetsType([]))

        # enable ip-fabric-forwarding
        if provider:
            vn.add_virtual_network(provider)

        # Update VN.
        self._vnc_lib.virtual_network_update(vn)

        # Cache the virtual network.
        VirtualNetworkKM.locate(vn_uuid)

        return vn

    def _delete_isolated_ns_virtual_network(self, ns_name, vn_name,
                                            proj_fq_name):
        """
        Delete the virtual network associated with this namespace.
        """
        # First lookup the cache for the entry.
        vn = VirtualNetworkKM.find_by_name_or_uuid(vn_name)
        if not vn:
            return

        try:
            vn_obj = self._vnc_lib.virtual_network_read(fq_name=vn.fq_name)
            # Delete/cleanup network policy allocated for this network.
            network_policy_refs = vn_obj.get_network_policy_refs()
            if network_policy_refs:
                for network_policy_ref in network_policy_refs:
                    self._vnc_lib. \
                        network_policy_delete(id=network_policy_ref['uuid'])
            # Delete/cleanup ipams allocated for this network.
            ipam_refs = vn_obj.get_network_ipam_refs()
            if ipam_refs:
                proj_obj = self._vnc_lib.project_read(fq_name=proj_fq_name)
                for ipam in ipam_refs:
                    ipam_obj = NetworkIpam(
                        name=ipam['to'][-1], parent_obj=proj_obj)
                    vn_obj.del_network_ipam(ipam_obj)
                    self._vnc_lib.virtual_network_update(vn_obj)
        except NoIdError:
            pass

        # Delete the network.
        self._vnc_lib.virtual_network_delete(id=vn.uuid)

        # Delete the network from cache.
        VirtualNetworkKM.delete(vn.uuid)

    def _create_policy(self, policy_name, proj_obj, src_vn_obj, dst_vn_obj):
        policy_exists = False
        policy = NetworkPolicy(name=policy_name, parent_obj=proj_obj)
        try:
            policy_obj = self._vnc_lib.network_policy_read(
                fq_name=policy.get_fq_name())
            policy_exists = True
        except NoIdError:
            # policy does not exist. Create one.
            policy_obj = policy
        network_policy_entries = PolicyEntriesType(
            [PolicyRuleType(
                direction = '<>',
                action_list = ActionListType(simple_action='pass'),
                protocol = 'any',
                src_addresses = [
                    AddressType(virtual_network = src_vn_obj.get_fq_name_str())
                ],
                src_ports = [PortType(-1, -1)],
                dst_addresses = [
                    AddressType(virtual_network = dst_vn_obj.get_fq_name_str())
                ],
                dst_ports = [PortType(-1, -1)])
            ])
        policy_obj.set_network_policy_entries(network_policy_entries)
        if policy_exists:
            self._vnc_lib.network_policy_update(policy)
        else:
            self._vnc_lib.network_policy_create(policy)
        return policy_obj

    def _attach_policy(self, vn_obj, *policies):
        for policy in policies or []:
            vn_obj.add_network_policy(policy, \
                VirtualNetworkPolicyType(sequence=SequenceType(0, 0)))
        self._vnc_lib.virtual_network_update(vn_obj)
        for policy in policies or []:
            self._vnc_lib.ref_relax_for_delete(vn_obj.uuid, policy.uuid)

    def _create_attach_policy(self, proj_obj, ip_fabric_vn_obj, \
            pod_vn_obj, service_vn_obj):
        policy_name = '%s-%s-default' \
            %(ip_fabric_vn_obj.name, pod_vn_obj.name)
        ip_fabric_pod_policy = self._create_policy(policy_name, proj_obj, \
            ip_fabric_vn_obj, pod_vn_obj)
        policy_name = '%s-%s-default' \
            %(ip_fabric_vn_obj.name, service_vn_obj.name)
        ip_fabric_service_policy = self._create_policy(policy_name, proj_obj, \
            ip_fabric_vn_obj, service_vn_obj)
        policy_name = '%s-%s-default'\
            %(service_vn_obj.name, pod_vn_obj.name)
        service_pod_policy = self._create_policy(policy_name, proj_obj, \
            service_vn_obj, pod_vn_obj)
        self._attach_policy(ip_fabric_vn_obj, \
            ip_fabric_pod_policy, ip_fabric_service_policy)
        self._attach_policy(pod_vn_obj, \
            ip_fabric_pod_policy, service_pod_policy)
        self._attach_policy(service_vn_obj, \
            ip_fabric_service_policy, service_pod_policy)

    def _update_security_groups(self, ns_name, proj_obj, network_policy):
        def _get_rule(ingress, sg, prefix, ethertype):
            sgr_uuid = str(uuid.uuid4())
            if sg:
                if ':' not in sg:
                    sg_fq_name = proj_obj.get_fq_name_str() + ':' + sg
                else:
                    sg_fq_name = sg
                addr = AddressType(security_group=sg_fq_name)
            elif prefix:
                addr = AddressType(subnet=SubnetType(prefix, 0))
            local_addr = AddressType(security_group='local')
            if ingress:
                src_addr = addr
                dst_addr = local_addr
            else:
                src_addr = local_addr
                dst_addr = addr
            rule = PolicyRuleType(rule_uuid=sgr_uuid, direction='>',
                                  protocol='any',
                                  src_addresses=[src_addr],
                                  src_ports=[PortType(0, 65535)],
                                  dst_addresses=[dst_addr],
                                  dst_ports=[PortType(0, 65535)],
                                  ethertype=ethertype)
            return rule

        sg_dict = {}
        # create default security group
        sg_name = "-".join([vnc_kube_config.cluster_name(), ns_name, 'default'])
        DEFAULT_SECGROUP_DESCRIPTION = "Default security group"
        id_perms = IdPermsType(enable=True,
                               description=DEFAULT_SECGROUP_DESCRIPTION)

        rules = []
        ingress = True
        egress = True
        if network_policy and 'ingress' in network_policy:
            ingress_policy = network_policy['ingress']
            if ingress_policy and 'isolation' in ingress_policy:
                isolation = ingress_policy['isolation']
                if isolation == 'DefaultDeny':
                    ingress = False
        if ingress:
            rules.append(_get_rule(True, None, '0.0.0.0', 'IPv4'))
            rules.append(_get_rule(True, None, '::', 'IPv6'))
        if egress:
            rules.append(_get_rule(False, None, '0.0.0.0', 'IPv4'))
            rules.append(_get_rule(False, None, '::', 'IPv6'))
        sg_rules = PolicyEntriesType(rules)

        sg_obj = SecurityGroup(name=sg_name, parent_obj=proj_obj,
                               id_perms=id_perms,
                               security_group_entries=sg_rules)

        SecurityGroupKM.add_annotations(self, sg_obj, namespace=ns_name,
                                        name=sg_obj.name,
                                        k8s_type=self._k8s_event_type)
        try:
            self._vnc_lib.security_group_create(sg_obj)
            self._vnc_lib.chown(sg_obj.get_uuid(), proj_obj.get_uuid())
        except RefsExistError:
            self._vnc_lib.security_group_update(sg_obj)
        sg_obj = self._vnc_lib.security_group_read(sg_obj.fq_name)
        sg_uuid = sg_obj.get_uuid()
        SecurityGroupKM.locate(sg_uuid)
        sg_dict[sg_name] = sg_uuid

        # create namespace security group
        ns_sg_name = "-".join([vnc_kube_config.cluster_name(), ns_name, 'sg'])
        NAMESPACE_SECGROUP_DESCRIPTION = "Namespace security group"
        id_perms = IdPermsType(enable=True,
                               description=NAMESPACE_SECGROUP_DESCRIPTION)
        sg_obj = SecurityGroup(name=ns_sg_name, parent_obj=proj_obj,
                               id_perms=id_perms,
                               security_group_entries=None)

        SecurityGroupKM.add_annotations(self, sg_obj, namespace=ns_name,
                                        name=sg_obj.name,
                                        k8s_type=self._k8s_event_type)
        try:
            self._vnc_lib.security_group_create(sg_obj)
            self._vnc_lib.chown(sg_obj.get_uuid(), proj_obj.get_uuid())
        except RefsExistError:
            pass
        sg_obj = self._vnc_lib.security_group_read(sg_obj.fq_name)
        sg_uuid = sg_obj.get_uuid()
        SecurityGroupKM.locate(sg_uuid)
        sg_dict[ns_sg_name] = sg_uuid

        return sg_dict

    def vnc_namespace_add(self, namespace_id, name, labels):
        isolated_ns_ann = 'True' if self._is_namespace_isolated(name) \
            else 'False'
        proj_fq_name = vnc_kube_config.cluster_project_fq_name(name)
        proj_obj = Project(name=proj_fq_name[-1], fq_name=proj_fq_name)

        ProjectKM.add_annotations(self, proj_obj, namespace=name, name=name,
                                  k8s_uuid=(namespace_id),
                                  isolated=isolated_ns_ann)
        try:
            self._vnc_lib.project_create(proj_obj)
        except RefsExistError:
            proj_obj = self._vnc_lib.project_read(fq_name=proj_fq_name)
        project = ProjectKM.locate(proj_obj.uuid)

        # Validate the presence of annotated virtual network.
        ann_vn_fq_name = self._get_annotated_virtual_network(name)
        if ann_vn_fq_name:
            # Validate that VN exists.
            try:
                self._vnc_lib.virtual_network_read(ann_vn_fq_name)
            except NoIdError as e:
                self._logger.error(
                    "Unable to locate virtual network [%s]"
                    "annotated on namespace [%s]. Error [%s]" %\
                    (ann_vn_fq_name, name, str(e)))
                return None

        # If this namespace is isolated, create it own network.
        if self._is_namespace_isolated(name) == True:
            vn_name = self._get_namespace_pod_vn_name(name)
            ipam_fq_name = vnc_kube_config.pod_ipam_fq_name()
            ipam_obj = self._vnc_lib.network_ipam_read(fq_name=ipam_fq_name)
            pod_vn = self._create_isolated_ns_virtual_network( \
                    ns_name=name, vn_name=vn_name, proj_obj=proj_obj,
                    ipam_obj=ipam_obj, provider=self._ip_fabric_vn_obj)
            # Cache pod network info in namespace entry.
            self._set_namespace_pod_virtual_network(name, pod_vn.get_fq_name())
            vn_name = self._get_namespace_service_vn_name(name)
            ipam_fq_name = vnc_kube_config.service_ipam_fq_name()
            ipam_obj = self._vnc_lib.network_ipam_read(fq_name=ipam_fq_name)
            service_vn = self._create_isolated_ns_virtual_network( \
                    ns_name=name, vn_name=vn_name,
                    ipam_obj=ipam_obj,proj_obj=proj_obj)
            # Cache service network info in namespace entry.
            self._set_namespace_service_virtual_network(name, service_vn.get_fq_name())
            self._create_attach_policy(proj_obj, self._ip_fabric_vn_obj, \
                    pod_vn, service_vn)

        try:
            network_policy = self._get_network_policy_annotations(name)
            sg_dict = self._update_security_groups(name, proj_obj,
                                                   network_policy)
            self._ns_sg[name] = sg_dict
        except RefsExistError:
            pass

        if project:
            self._update_namespace_label_cache(labels, namespace_id, project)
        return project

    def vnc_namespace_delete(self, namespace_id, name):
        proj_fq_name = vnc_kube_config.cluster_project_fq_name(name)
        project_uuid = ProjectKM.get_fq_name_to_uuid(proj_fq_name)
        if not project_uuid:
            self._logger.error("Unable to locate project for k8s namespace "
                               "[%s]" % (name))
            return

        project = ProjectKM.get(project_uuid)
        if not project:
            self._logger.error("Unable to locate project for k8s namespace "
                               "[%s]" % (name))
            return

        default_sg_fq_name = proj_fq_name[:]
        sg = "-".join([vnc_kube_config.cluster_name(), name, 'default'])
        default_sg_fq_name.append(sg)
        ns_sg_fq_name = proj_fq_name[:]
        ns_sg = "-".join([vnc_kube_config.cluster_name(), name, 'sg'])
        ns_sg_fq_name.append(ns_sg)
        sg_list = [default_sg_fq_name, ns_sg_fq_name]

        try:
            # If the namespace is isolated, delete its virtual network.
            if self._is_namespace_isolated(name):
                vn_name = self._get_namespace_pod_vn_name(name)
                self._delete_isolated_ns_virtual_network(
                    name, vn_name=vn_name, proj_fq_name=proj_fq_name)
                # Clear pod network info from namespace entry.
                self._set_namespace_pod_virtual_network(name, None)
                vn_name = self._get_namespace_service_vn_name(name)
                self._delete_isolated_ns_virtual_network(
                    name, vn_name=vn_name, proj_fq_name=proj_fq_name)
                # Clear service network info from namespace entry.
                self._set_namespace_service_virtual_network(name, None)

            # delete default-sg and ns-sg security groups
            security_groups = project.get_security_groups()
            for sg_uuid in security_groups:
                sg = SecurityGroupKM.get(sg_uuid)
                if sg and sg.fq_name in sg_list[:]:
                    self._vnc_lib.security_group_delete(id=sg_uuid)
                    sg_list.remove(sg.fq_name)
                    if not len(sg_list):
                        break

            # delete the label cache
            if project:
                self._clear_namespace_label_cache(namespace_id, project)
            # delete the namespace
            self._delete_namespace(name)

            # If namespace=project, delete the project
            if vnc_kube_config.cluster_project_name(name) == name:
                self._vnc_lib.project_delete(fq_name=proj_fq_name)
        except:
            pass

    def _sync_namespace_project(self):
        """Sync vnc project objects with K8s namespace object.

        This method walks vnc project local cache and validates that
        a kubernetes namespace object exists for this project.
        If a kubernetes namespace object is not found for this project,
        then construct and simulates a delete event for the namespace,
        so the vnc project can be cleaned up.
        """
        for project in ProjectKM.objects():
            k8s_namespace_uuid = project.get_k8s_namespace_uuid()
            # Proceed only if this project is tagged with a k8s namespace.
            if k8s_namespace_uuid and not\
                   self._get_namespace(k8s_namespace_uuid):
                event = {}
                dict_object = {}
                dict_object['kind'] = 'Namespace'
                dict_object['metadata'] = {}
                dict_object['metadata']['uid'] = k8s_namespace_uuid
                dict_object['metadata']['name'] = project.get_k8s_namespace_name()

                event['type'] = 'DELETED'
                event['object'] = dict_object
                self._queue.put(event)

    def namespace_timer(self):
        self._sync_namespace_project()

    def process(self, event):
        event_type = event['type']
        kind = event['object'].get('kind')
        name = event['object']['metadata'].get('name')
        ns_id = event['object']['metadata'].get('uid')
        labels = event['object']['metadata'].get('labels', {})
        print("%s - Got %s %s %s:%s"
              %(self._name, event_type, kind, name, ns_id))
        self._logger.debug("%s - Got %s %s %s:%s"
                           %(self._name, event_type, kind, name, ns_id))

        if event['type'] == 'ADDED' or event['type'] == 'MODIFIED':
            self.vnc_namespace_add(ns_id, name, labels)
            self._network_policy_mgr.update_ns_np(name, ns_id, labels,
                                                  self._ns_sg[name])
        elif event['type'] == 'DELETED':
            self.vnc_namespace_delete(ns_id, name)
        else:
            self._logger.warning(
                'Unknown event type: "{}" Ignoring'.format(event['type']))
