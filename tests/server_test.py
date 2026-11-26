"""Drive tf3_mcp_server.py over stdio against the exchange folder left by bridge_test.py (first arg:
<bridge_test output>/mod_presets)."""
import json, os, subprocess, sys

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "tf3_mcp_server.py")
env = dict(os.environ, TF3_MCP_BRIDGE_DIR=sys.argv[1], TF3_MCP_TIMEOUT="0.5")
p = subprocess.Popen([sys.executable, "-I", SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                     text=True, encoding="utf-8")

def rpc(i, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if i is not None:
        msg["id"] = i
    if params is not None:
        msg["params"] = params
    p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()
    if i is None:
        return None
    return json.loads(p.stdout.readline())

ok = True
def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)

r = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
check("initialize", r["result"]["serverInfo"]["name"] == "tf3-mcp-bridge")
rpc(None, "notifications/initialized")
r = rpc(2, "tools/list")
names = [t["name"] for t in r["result"]["tools"]]
check("14 tools", names == ["tf3_status", "tf3_list_lines", "tf3_list_towns", "tf3_route_ideas", "tf3_get_line", "tf3_list_vehicle_models", "tf3_list_industries", "tf3_list_stations", "tf3_list_depots", "tf3_list_warehouses", "tf3_stale_cargo", "tf3_finances", "tf3_live_vehicles", "tf3_fleet_issues"])

def call(i, name, args=None):
    r = rpc(i, "tools/call", {"name": name, "arguments": args or {}})
    res = r["result"]
    text = res["content"][0]["text"]
    return res.get("isError", False), (text if res.get("isError") else json.loads(text))

err, s = call(3, "tf3_status")
check("status (fallback answer, warned)", not err and s["lines"] == 2 and s["towns"] == 2 and "did not answer" in s.get("warning", ""))
err, s = call(4, "tf3_list_lines", {"sort_by": "profit12m"})
check("list_lines sorted", not err and [l["name"] for l in s["lines"]] == ["Line A", "Line B"])
err, s = call(5, "tf3_list_lines", {"sort_by": "profit12m", "ascending": True})
check("list_lines ascending", not err and [l["name"] for l in s["lines"]] == ["Line B", "Line A"])
err, s = call(6, "tf3_get_line", {"line": "line a"})
check("get_line by name", not err and s["id"] == 100 and s["unitsByModel"] == {"City Bus": 2} and len(s["vehicles"]) == 2)
err, s = call(7, "tf3_get_line", {"line": "line"})
check("ambiguous name is a tool error", err and "several" in s)
err, s = call(8, "tf3_list_vehicle_models", {})
check("models available only", not err and [m["name"] for m in s["models"]] == ["City Bus"])
err, s = call(9, "tf3_list_vehicle_models", {"available_only": False})
check("models all", not err and s["totalMatching"] == 2)
err, s = call(10, "tf3_fleet_issues", {})
check("fleet issues", not err and {l["name"] for l in s["lines"]} == {"Line A", "Line B"} and s["vehiclesFlagged"] == 0)
print(json.dumps(s, indent=1)[:1500])
err, s = call(12, "tf3_list_towns", {})
check("towns sorted by population", not err and [t["name"] for t in s["towns"]] == ["Springfield", "Zürich"] and s["towns"][0]["linesServing"] == 2)
err, s = call(13, "tf3_list_towns", {"name_contains": "zür"})
check("town filter lists serving lines", not err and {l["name"] for l in s["towns"][0]["lines"]} == {"Line A"})
err, s = call(14, "tf3_list_lines", {"town": "springfield"})
check("lines by town", not err and [l["name"] for l in s["lines"]] == ["Line A", "Line B"] and s["lines"][0]["towns"] == ["Springfield", "Zürich"])
r = rpc(11, "bogus/method")
check("unknown method error", r.get("error", {}).get("code") == -32601)
p.stdin.close(); p.wait(5)
sys.exit(0 if ok else 1)
