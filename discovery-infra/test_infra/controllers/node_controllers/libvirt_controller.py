import os
import logging
import libvirt
import waiting
from xml.dom import minidom
from contextlib import suppress

from test_infra import utils
from test_infra import consts
from test_infra.controllers.node_controllers.node_controller import NodeController


class LibvirtController(NodeController):

    def __init__(self, **kwargs):
        self.libvirt_connection = libvirt.open('qemu:///system')
        self.private_ssh_key_path = kwargs.get("private_ssh_key_path")

    def __del__(self):
        with suppress(Exception):
            self.libvirt_connection.close()

    def list_nodes(self):
        return self.list_nodes_with_name_filter(None)

    def list_nodes_with_name_filter(self, name_filter):
        logging.info("Listing current hosts with name filter %s", name_filter)
        nodes = []
        domains = self.libvirt_connection.listAllDomains()
        for domain in domains:
            domain_name = domain.name()
            if name_filter and name_filter not in domain_name:
                continue
            if (consts.NodeRoles.MASTER in domain_name) or (consts.NodeRoles.WORKER in domain_name):
                nodes.append(domain)
        logging.info("Found domains %s", nodes)
        return nodes

    def list_networks(self):
        return self.libvirt_connection.listAllNetworks()

    def list_leases(self, network_name):
        return self.libvirt_connection.networkLookupByName(network_name).DHCPLeases()

    def shutdown_node(self, node_name):
        logging.info("Going to shutdown %s", node_name)
        node = self.libvirt_connection.lookupByName(node_name)

        if node.isActive():
            node.destroy()

    def shutdown_all_nodes(self):
        logging.info("Going to shutdown all the nodes")
        nodes = self.list_nodes()

        for node in nodes:
            self.shutdown_node(node.name())

    def start_node(self, node_name):
        logging.info("Going to power-on %s", node_name)
        node = self.libvirt_connection.lookupByName(node_name)

        if not node.isActive():
            try:
                node.create()
                self._wait_till_domain_has_ips(node)
            except waiting.exceptions.TimeoutExpired:
                 logging.warning("Node %s failed to recive IP, retrying", node_name)
                 self.shutdown_node(node_name)
                 node.create()
                 self._wait_till_domain_has_ips(node)

    def start_all_nodes(self):
        logging.info("Going to power-on all the nodes")
        nodes = self.list_nodes()

        for node in nodes:
            self.start_node(node.name())
        return nodes

    @staticmethod
    def format_disk(disk_path):
        logging.info("Formatting disk %s", disk_path)
        if not os.path.exists(disk_path):
            logging.info("Path to %s disk not exists. Skipping", disk_path)
            return

        command = f"qemu-img info {disk_path} | grep 'virtual size'"
        output = utils.run_command(command, shell=True)
        image_size = output[0].split(' ')[2]
        # Fix for libvirt 6.0.0
        if image_size.isdigit():
            image_size += "G"
        command = f'qemu-img create -f qcow2 {disk_path} {image_size}'
        utils.run_command(command, shell=True)

    def restart_node(self, node_name):
        logging.info("Restarting %s", node_name)
        self.shutdown_node(node_name=node_name)
        self.start_node(node_name=node_name)

    def format_all_node_disks(self):
        logging.info("Formatting all the disks")
        nodes = self.list_nodes()

        for node in nodes:
            self.format_node_disk(node.name())

    def prepare_nodes(self):
        self.destroy_all_nodes()

    def destroy_all_nodes(self):
        logging.info("Delete all the nodes")
        self.shutdown_all_nodes()
        self.format_all_node_disks()

    def is_active(self, node_name):
        node = self.libvirt_connection.lookupByName(node_name)
        return node.isActive()

    def get_node_ips_and_macs(self, node_name):
        node = self.libvirt_connection.lookupByName(node_name)
        return self._get_domain_ips_and_macs(node)

    def _get_domain_ips_and_macs(self, domain):
        interfaces = domain.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE)
        ips = []
        macs = []
        if interfaces:
            for (_, val) in interfaces.items():
                if val['addrs']:
                    for addr in val['addrs']:
                        ips.append(addr['addr'])
                        macs.append(val['hwaddr'])
        if ips:
            logging.info("Host %s ips are %s", domain.name(), ips)
        if macs:
            logging.info("Host %s macs are %s", domain.name(), macs)
        return ips, macs

    def _get_domain_ips(self, domain):
        ips, _ = self._get_domain_ips_and_macs(domain)
        return ips

    def _wait_till_domain_has_ips(self, domain, timeout=360, interval=5):
        logging.info("Waiting till host %s will have ips", domain.name())
        waiting.wait(
            lambda: len(self._get_domain_ips(domain)) > 0,
            timeout_seconds=timeout,
            sleep_seconds=interval,
            waiting_for="Waiting for Ips",
            expected_exceptions=Exception
        )

    def set_boot_order(self, node_name, cd_first=False):
        logging.info(f"Going to set the following boot order: cd_first: {cd_first}, "
                     f"for node: {node_name}")
        node = self.libvirt_connection.lookupByName(node_name)
        current_xml = node.XMLDesc(0)
        # Creating XML obj
        xml = minidom.parseString(current_xml.encode('utf-8'))
        os_element = xml.getElementsByTagName('os')[0]
        # Delete existing boot elements
        for el in os_element.getElementsByTagName('boot'):
            dev = el.getAttribute('dev')
            if dev in ['cdrom', 'hd']:
                os_element.removeChild(el)
            else:
                raise ValueError(f'Found unexpected boot device: \'{dev}\'')
        # Set boot elements for hd and cdrom
        first = xml.createElement('boot')
        first.setAttribute('dev', 'cdrom' if cd_first else 'hd')
        os_element.appendChild(first)
        second = xml.createElement('boot')
        second.setAttribute('dev', 'hd' if cd_first else 'cdrom')
        os_element.appendChild(second)
        # Apply new machine xml
        dom = self.libvirt_connection.defineXML(xml.toprettyxml())
        if dom is None:
            raise Exception(f"Failed to set boot order cdrom first: {cd_first}, "
                            f"for node: {node_name}")
        logging.info(f"Boot order set successfully: cdrom first: {cd_first}, "
                     f"for node: {node_name}")

    def get_host_id(self, node_name):
        dom = self.libvirt_connection.lookupByName(node_name)
        return dom.UUIDString()

    def get_cpu_cores(self, node_name):
        xml = self._get_xml(node_name)
        vcpu_element = xml.getElementsByTagName('vcpu')[0]
        return int(vcpu_element.firstChild.nodeValue)

    def set_cpu_cores(self, node_name, core_count):
        logging.info(f"Going to set vcpus to {core_count} for node: {node_name}")
        dom = self.libvirt_connection.lookupByName(node_name)
        dom.setVcpusFlags(core_count)
        logging.info(f"Successfully set vcpus to {core_count} for node: {node_name}")

    def get_ram_kib(self, node_name):
        xml = self._get_xml(node_name)
        memory_element = xml.getElementsByTagName('currentMemory')[0]
        return int(memory_element.firstChild.nodeValue)

    def set_ram_kib(self, node_name, ram_kib):
        logging.info(f"Going to set memory to {ram_kib} for node: {node_name}")
        xml = self._get_xml(node_name)
        memory_element = xml.getElementsByTagName('memory')[0]
        memory_element.firstChild.replaceWholeText(ram_kib)
        current_memory_element = xml.getElementsByTagName('currentMemory')[0]
        current_memory_element.firstChild.replaceWholeText(ram_kib)
        dom = self.libvirt_connection.defineXML(xml.toprettyxml())
        if dom is None:
            raise Exception(f"Failed to set memory for node: {node_name}")
        logging.info(f"Successfully set memory to {ram_kib} for node: {node_name}")

    def _get_xml(self, node_name):
        dom = self.libvirt_connection.lookupByName(node_name)
        current_xml = dom.XMLDesc(0)
        return minidom.parseString(current_xml.encode('utf-8'))