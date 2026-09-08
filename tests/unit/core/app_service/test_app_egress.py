"""032 — egress deny is an nft table per app bridge, never '--network none'."""
import asyncio

import pytest

from core.app_service.egress import EgressApplier, render_rules, resolve_allowlist, table_name


def test_none_drops_all_but_replies():
    s = render_rules(slug="rob-status", subnet="172.30.5.0/24", mode="none", allow_addrs=[])
    assert "table inet polyrob_app_rob_status {" in s
    assert "ip saddr 172.30.5.0/24 ct state established,related accept" in s
    assert "ip saddr 172.30.5.0/24 drop" in s
    assert "daddr" not in s
    assert s.index("established,related accept") < s.index(" drop")


def test_allowlist_admits_resolved_addresses_only():
    s = render_rules(slug="st", subnet="172.30.5.0/24", mode="allowlist",
                     allow_addrs=["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"])
    assert "ip saddr 172.30.5.0/24 ip daddr { 93.184.216.34 } accept" in s
    assert "ip6 daddr { 2606:2800:220:1:248:1893:25c8:1946 } accept" in s
    assert s.rstrip().endswith("}")


def test_open_renders_nothing_and_bad_inputs_refuse():
    assert render_rules(slug="st", subnet="10.0.0.0/24", mode="open", allow_addrs=[]) == ""
    with pytest.raises(ValueError):
        render_rules(slug="st", subnet="not-a-net", mode="none", allow_addrs=[])
    with pytest.raises(ValueError):
        render_rules(slug="st", subnet="10.0.0.0/24", mode="allowlist", allow_addrs=["1.2.3.4; drop"])
    with pytest.raises(ValueError):
        render_rules(slug="st", subnet="10.0.0.0/24", mode="everything", allow_addrs=[])
    with pytest.raises(ValueError):
        table_name("a;b")


def test_resolve_allowlist_uses_injected_resolver_and_skips_junk():
    def resolver(h):
        return {"api.example.com": ["1.1.1.1", "1.1.1.1", "2606::1"]}.get(h, [])
    out = asyncio.run(resolve_allowlist(["api.example.com", "bad host", "nope.invalid"],
                                        resolver=resolver))
    assert out.addrs == ["1.1.1.1", "2606::1"]
    assert [r.split(" ")[0] for r in out.refused] == ["bad", "nope.invalid"]


@pytest.mark.parametrize("addr", [
    "169.254.169.254",      # AWS/GCP/Azure metadata
    "fd00:ec2::254",        # AWS IMDS over IPv6
    "100.100.100.200",      # Alibaba metadata (CGNAT, not is_private everywhere)
    "127.0.0.1", "::1",
    "10.1.2.3", "192.168.1.10", "172.16.9.9", "fc00::1",
    "169.254.1.1", "fe80::1",
    "100.64.0.7",           # CGNAT
    "224.0.0.1", "ff02::1", # multicast
    "240.0.0.1",            # reserved
    "0.0.0.0", "::",
    "::ffff:10.0.0.1",      # IPv4-mapped private
])
def test_private_and_metadata_addresses_are_never_admitted(addr):
    """An allow-host that resolves (now or after a rebind) to an internal address
    must NOT earn an ACCEPT rule out of the sandbox."""
    from core.app_service.egress import is_public_addr
    assert is_public_addr(addr) is False
    out = asyncio.run(resolve_allowlist(["metadata.example.com"], resolver=lambda h: [addr]))
    assert out.addrs == []
    assert out.refused and "metadata.example.com" in out.refused[0]
    assert "private" in out.refused[0]
    s = render_rules(slug="st", subnet="172.30.5.0/24", mode="allowlist", allow_addrs=[addr])
    assert addr not in s and "daddr" not in s


def test_public_addresses_still_resolve_and_render():
    from core.app_service.egress import is_public_addr
    assert is_public_addr("93.184.216.34") is True
    assert is_public_addr("2606:2800:220:1:248:1893:25c8:1946") is True
    assert is_public_addr("not-an-ip") is False
    out = asyncio.run(resolve_allowlist(["api.example.com"],
                                        resolver=lambda h: ["10.0.0.1", "93.184.216.34"]))
    assert out.addrs == ["93.184.216.34"] and out.refused == []


class _Runner:
    def __init__(self):
        self.calls = []

    async def __call__(self, argv, *, input=None, timeout=None):
        self.calls.append((list(argv), input))
        if argv[:2] == ["nft", "delete"]:
            return (1, "", "Error: No such file or directory")
        return (0, "", "")


def test_applier_pipes_script_and_removes_for_open():
    r = _Runner()
    a = EgressApplier(r)
    ok, _ = asyncio.run(a.apply(slug="st", subnet="10.0.0.0/24", mode="none", allow_addrs=[]))
    assert ok and r.calls[0][0] == ["nft", "-f", "-"] and "drop" in r.calls[0][1]
    ok, _ = asyncio.run(a.apply(slug="st", subnet="10.0.0.0/24", mode="open", allow_addrs=[]))
    assert ok and r.calls[1][0] == ["nft", "delete", "table", "inet", "polyrob_app_st"]


def test_table_present_asks_the_kernel():
    class _R:
        def __init__(self, code):
            self.code, self.calls = code, []

        async def __call__(self, argv, *, input=None, timeout=None):
            self.calls.append(list(argv))
            return (self.code, "", "" if self.code == 0 else "No such file or directory")

    r = _R(0)
    assert asyncio.run(EgressApplier(r).table_present("rob-status")) is True
    assert r.calls == [["nft", "list", "table", "inet", "polyrob_app_rob_status"]]
    assert asyncio.run(EgressApplier(_R(1)).table_present("rob-status")) is False
