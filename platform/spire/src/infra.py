import access
import configuration
import command
import setup
import ssh


def approve_admit(server_principal: str, fingerprint: str) -> str:
    config = configuration.get_config()
    principal_hostname = config.get_fqdn(server_principal)
    # TODO: unify keyreq and keyinitadmit
    if config.is_kerberos_enabled():
        result = access.call_keyreq("admit", principal_hostname, fingerprint).decode().strip()
    else:
        result = ssh.check_ssh_output(config.keyserver, "keyinitadmit", principal_hostname, fingerprint).decode().strip()
    if len(result) != 0:
        raise Exception("expected empty output, not %s" % repr(result))


def infra_admit(server_principal: str, fingerprint: str) -> None:
    approve_admit(server_principal, fingerprint)
    print("Approved admission for", server_principal)


def infra_list_admits() -> None:
    config = configuration.get_config()
    print("Retrieving admission requests...")
    if config.is_kerberos_enabled():
        print(access.call_keyreq("list-requests").decode().strip())
    else:
        print(ssh.check_ssh_output(config.keyserver, "keyinitadmit").decode().strip())

# TODO: interactive admission command, which goes through them and asks you to verify each one


def infra_install_packages(ops: setup.Operations) -> None:
    config = configuration.get_config()
    for node in config.nodes:
        ops.ssh("update apt repositories on @HOST", node, "apt-get", "update")
        ops.ssh("upgrade packages on @HOST", node, "apt-get", "dist-upgrade", "-y")


def infra_sync(ops: setup.Operations, node_name: str) -> None:
    node = configuration.get_config().get_node(node_name)
    ops.ssh("synchronize operations on @HOST", node, "sync")


def infra_sync_supervisor(ops: setup.Operations) -> None:
    infra_sync(ops, configuration.get_config().keyserver.hostname)


main_command = command.mux_map("commands about maintaining the infrastructure of a cluster", {
    "admit": command.wrap("approve admitting a node to the cluster", infra_admit),
    "requests": command.wrap("list requests for joining nodes to the cluster", infra_list_admits),
    "install-packages": setup.wrapop("install and update packages on a node", infra_install_packages),
    "sync": setup.wrapop("synchronize the filesystem to disk on a node", infra_sync),
    "sync-supervisor": setup.wrapop("synchronize the filesystem to disk on the supervisor", infra_sync_supervisor),
})
