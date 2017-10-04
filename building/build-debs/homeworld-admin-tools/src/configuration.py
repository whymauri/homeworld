import os

import util
import command
import resource
import subprocess
import template
import yaml


def get_project() -> str:
    project_dir = os.getenv("HOMEWORLD_DIR")
    if project_dir is None:
        command.fail("no HOMEWORLD_DIR environment variable declared")
    if not os.path.isdir(project_dir):
        command.fail("HOMEWORLD_DIR (%s) is not a directory that exists" % project_dir)
    return project_dir


def get_editor() -> str:
    return os.getenv("EDITOR", "nano")


class CIDR:
    def __init__(self, cidr: str):
        if cidr.count("/") != 1:
            raise Exception("invalid cidr: %s" % cidr)
        ip, bits = cidr.split("/")
        bits = int(bits)
        if bits < 0 or bits > 32:
            raise Exception("invalid bits: %s" % bits)
        self.ip = IP(ip)
        self.bits = bits
        self.cidr = "%s/%d" % (self.ip.ip, self.bits)

    def netmask(self) -> "IP":
        return IP.from_integer(((1 << (32 - self.bits)) - 1) ^ 0xFFFFFFFF)

    def __contains__(self, ip):
        return (ip & self.netmask()) == self.ip

    def __str__(self):
        return self.cidr

    def __repr__(self):
        return "cidr:" + self.cidr


class IP:
    def __init__(self, ip: str):
        if ip.count(".") != 3:
            raise Exception("invalid ipv4 address: %s" % ip)
        self.octets = [int(x) for x in ip.split(".")]
        for octet in self.octets:
            if octet < 0 or octet >= 256:
                raise Exception("invalid ipv4 address: bad octet %s" % octet)
        self.ip = "%d.%d.%d.%d" % tuple(self.octets)

    @classmethod
    def from_integer(cls, num: int) -> "IP":
        assert 0 <= num <= 0xFFFFFFFF
        octets = (((num & 0xFF000000) >> 24),
                  ((num & 0x00FF0000) >> 16),
                  ((num & 0x0000FF00) >> 8),
                  ((num & 0x000000FF) >> 0))
        return IP("%d.%d.%d.%d" % octets)

    def __hash__(self):
        return hash(self.ip)

    def __eq__(self, other):
        if isinstance(other, IP):
            return self.ip == other.ip
        return NotImplemented

    def __ne__(self, other):
        if isinstance(other, IP):
            return self.ip != other.ip
        return NotImplemented

    def __and__(self, other):
        if isinstance(other, IP):
            return IP("%d.%d.%d.%d" % tuple(o1 & o2 for o1, o2 in zip(self.octets, other.octets)))
        else:
            return NotImplemented

    def __str__(self):
        return self.ip

    def __repr__(self):
        return "ip:" + self.ip


class Node:
    def __init__(self, config: dict):
        self.hostname, ip, self.kind = keycheck(config, "hostname", "ip", "kind")
        self.ip = IP(ip)

        if self.kind not in {"master", "worker", "supervisor"}:
            raise Exception("invalid node kind: %s" % self.kind)

    def __repr__(self):
        return "%s node %s (%s)" % (self.kind, self.hostname, self.ip)


def keycheck(kvs: dict, *keys: str, validator=lambda k, v: True):
    for key in kvs.keys():
        if key not in keys:
            command.fail("unexpected key %s in config" % key)
    for key in keys:
        if key not in kvs:
            command.fail("could not find expected key %s in config" % key)
    for key, value in kvs.items():
        if not validator(key, value):
            command.fail("config failed validation: key %s had invalid value %s" % (key, value))
    return [kvs[k] for k in keys]


class Config:
    def __init__(self, kv: dict):
        v_cluster, v_addresses, v_dns_bootstrap, v_root_admins, v_nodes = \
            keycheck(kv, "cluster", "addresses", "dns-bootstrap", "root-admins", "nodes")

        self.external_domain, self.internal_domain, self.etcd_token = \
            keycheck(v_cluster, "external-domain", "internal-domain", "etcd-token",
                     validator=lambda _, x: type(x) == str)

        cidr_pods, cidr_services, service_api, service_dns = \
            keycheck(v_addresses, "cidr-pods", "cidr-services", "service-api", "service-dns",
                     validator=lambda _, x: type(x) == str)
        self.cidr_pods = CIDR(cidr_pods)
        self.cidr_services = CIDR(cidr_services)
        self.service_api = IP(service_api)
        self.service_dns = IP(service_dns)

        if self.service_api not in self.cidr_services or self.service_dns not in self.cidr_services:
            command.fail("in config: expected service IPs to be in the correct CIDR")

        self.dns_bootstrap = {hostname: IP(ip) for hostname, ip in v_dns_bootstrap.items()}

        self.root_admins = v_root_admins
        assert all(type(v) == str for v in self.root_admins)  # TODO: better error handling

        self.nodes = [Node(n) for n in v_nodes]

        self.keyserver = None

        for node in self.nodes:
            if node.kind == "supervisor":
                if self.keyserver is not None:
                    command.fail("in config: multiple supervisors not yet supported")
                self.keyserver = node

    @classmethod
    def load_from_string(cls, contents: bytes):
        return Config(yaml.safe_load(contents))

    @classmethod
    def load_from_file(cls, filepath: str):
        return Config.load_from_string(util.readfile(filepath))

    @classmethod
    def load_from_project(cls):
        return Config.load_from_file(os.path.join(get_project(), "setup.yaml"))


def get_keyserver_yaml() -> str:
    config = Config.load_from_project()

    nodes = [
        {
            "HOSTNAME": node.hostname,
            "DOMAIN": config.external_domain,
            "IP": node.ip,
            "KIND": node.kind,
            "SCHEDULE": "true" if node.kind == "worker" else "false"
        } for node in config.nodes]

    accounts = template.template_all(
        """
  - principal: {{HOSTNAME}}.{{DOMAIN}}
    group: {{KIND}}-nodes
    limit-ip: true
    metadata:
      ip: {{IP}}
      hostname: {{HOSTNAME}}
      schedule: {{SCHEDULE}}
      kind: {{KIND}}
        """.strip("\n"), nodes, load=False)

    admins = [{"PRINCIPAL": root_admin} for root_admin in config.root_admins]

    accounts += template.template_all(
        """
  - principal: {{PRINCIPAL}}
    disable-direct-auth: true
    group: root-admins
        """.strip("\n"), admins, load=False)

    ksrv = {"SERVICEAPI": config.service_api,
            "ACCOUNTS": "".join(accounts),
            "DOMAIN": config.external_domain,
            "INTERNAL-DOMAIN": config.internal_domain}

    return template.template("keyserver.yaml", ksrv)


KEYCLIENT_VARIANTS = ("master", "worker", "base", "supervisor")


def get_keyclient_yaml(variant: str) -> str:
    if variant not in KEYCLIENT_VARIANTS:
        command.fail("invalid variant %s; expected one of %s" % (variant, KEYCLIENT_VARIANTS))
    config = Config.load_from_project()
    kcli = {"KEYSERVER": config.keyserver.hostname + "." + config.external_domain,
            "MASTER": variant == "master",
            "WORKER": variant in ("worker", "master"),
            "BASE": variant == "base"}
    return template.template("keyclient.yaml", kcli)


def get_cluster_conf() -> str:
    config = Config.load_from_project()

    apiservers = [node for node in config.nodes if node.kind == "master"]

    cconf = {"APISERVER": "https://%s:443" % apiservers[0].ip,
             "APISERVER_COUNT": len(apiservers),
             "CLUSTER_CIDR": config.cidr_pods,
             "CLUSTER_DOMAIN": config.internal_domain,
             "DOMAIN": config.external_domain,
             "ETCD_CLUSTER": ",".join("%s=https://%s:2380" % (n.hostname, n.ip) for n in apiservers),
             "ETCD_ENDPOINTS": ",".join("https://%s:2379" % n.ip for n in apiservers),
             "ETCD_TOKEN": config.etcd_token,
             "SERVICE_API": config.service_api,
             "SERVICE_CIDR": config.cidr_services,
             "SERVICE_DNS": config.service_dns}

    output = ["# generated by spire from setup.yaml\n"]
    output += ["%s=%s\n" % kv for kv in sorted(cconf.items())]
    return "".join(output)


def get_machine_list_file() -> str:
    config = Config.load_from_project()
    return ",".join("%s.%s" % (node.hostname, config.external_domain) for node in config.nodes) + "\n"


def populate() -> None:
    setup_yaml = os.path.join(get_project(), "setup.yaml")
    if os.path.exists(setup_yaml):
        command.fail("setup.yaml already exists")
    resource.copy_to("setup.yaml", setup_yaml)
    print("filled out setup.yaml")


def edit() -> None:
    setup_yaml = os.path.join(get_project(), "setup.yaml")
    if not os.path.exists(setup_yaml):
        command.fail("setup.yaml does not exist (run spire config populate first?)")
    subprocess.check_call([get_editor(), "--", setup_yaml])


def print_keyserver_yaml() -> None:
    print(get_keyserver_yaml())


def print_keyclient_yaml(variant) -> None:
    print(get_keyclient_yaml(variant))


def print_cluster_conf() -> None:
    print(get_cluster_conf())


def print_machine_list_file() -> None:
    print(get_machine_list_file())


def gen_kube_specs(output_dir: str) -> None:
    if not os.path.isdir(output_dir):
        os.mkdir(output_dir)
    config = Config.load_from_project()

    vars = {"NETWORK": config.cidr_pods}

    clustered, = keycheck(yaml.safe_load(resource.get_resource("clustered/list.yaml")), "clustered",
                          validator=lambda _, x: type(x) == list)
    for configname in clustered:
        templated = template.template("clustered/%s" % configname, vars)
        util.writefile(os.path.join(output_dir, configname), templated.encode())


main_command = command.mux_map("commands about cluster configuration", {
    "populate": command.wrap("initialize the cluster's setup.yaml with the template", populate),
    "edit": command.wrap("open $EDITOR (defaults to nano) to edit the project's setup.yaml", edit),
    "gen-kube": command.wrap("generate kubernetes specs for the base cluster", gen_kube_specs),
    "show": command.mux_map("commands about showing different aspects of the configuration", {
        "keyserver.yaml": command.wrap("display the generated keyserver.yaml", print_keyserver_yaml),
        "keyclient.yaml": command.wrap("display the specified variant of keyclient.yaml", print_keyclient_yaml),
        "cluster.conf": command.wrap("display the generated cluster.conf", print_cluster_conf),
        "machine.list": command.wrap("display the generated machine.list", print_machine_list_file),
    }),
})