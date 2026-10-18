import os, tempfile, types
import watchers as W

class Eng:
    phone = None; busy = False; loop = None; suppressed = False
    def emit(self, *a): pass

d = tempfile.mkdtemp()
open(os.path.join(d, "old.zip"), "w").write("x")
w = W.Watchers(Eng(), d)
assert w._downloads(0) == []; w._primed = True
p = os.path.join(d, "new.zip"); open(p, "w").write("abc")
open(os.path.join(d, "x.crdownload"), "w").write("abc")
assert w._downloads(0) == []                       # first sight: still might grow
r = w._downloads(0)
assert len(r) == 1 and "new.zip" in r[0][2], r     # stable -> announced
assert w._downloads(0) == []                       # once only
# flap
bat = types.SimpleNamespace(power_plugged=True, percent=80)
W.psutil.sensors_battery = lambda: bat
w._power(0)
res = []
for i, plug in enumerate([False, True, False]):
    bat.power_plugged = plug; res += w._power(10 + i)
assert any(k == "flap" for k, *_ in res), res
bat.power_plugged = False; bat.percent = 10
assert any(k == "battery" for k, *_ in w._power(5000))
# disk hysteresis
W.psutil.disk_usage = lambda p: types.SimpleNamespace(free=10e9)
assert len(w._disk(0)) == 1 and w._disk(1) == []
W.psutil.disk_usage = lambda p: types.SimpleNamespace(free=40e9)
w._disk(2); assert w._disk_armed
# ram
W.psutil.virtual_memory = lambda: types.SimpleNamespace(percent=97)
assert [w._ram(9999 + i) for i in range(4)][-1]
print("ALL OK")
