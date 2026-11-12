import inspect, traceback
out = []
def log(*a): out.append(" ".join(str(x) for x in a))
try:
    from agentscope.app import create_app
    log("create_app signature:")
    log(inspect.signature(create_app))
except Exception:
    log("create_app import FAILED:\n" + traceback.format_exc())

import agentscope.app as app
log("agentscope.app exports:", [n for n in dir(app) if not n.startswith('_')])

# storage implementations
import agentscope.app.storage as st
log("storage exports:", [n for n in dir(st) if not n.startswith('_')])
for name in dir(st):
    if name.startswith('_'): continue
    obj = getattr(st, name)
    if inspect.isclass(obj):
        try: log(f"  class {name} sig:", inspect.signature(obj.__init__))
        except Exception as e: log(f"  class {name} sig err {e}")

# message bus
import agentscope.app.message_bus as mb
log("message_bus exports:", [n for n in dir(mb) if not n.startswith('_')])
for name in dir(mb):
    if name.startswith('_'): continue
    obj = getattr(mb, name)
    if inspect.isclass(obj):
        try: log(f"  class {name} sig:", inspect.signature(obj.__init__))
        except Exception as e: log(f"  class {name} sig err {e}")

# workspace manager
import agentscope.app.workspace_manager as wm
log("workspace_manager exports:", [n for n in dir(wm) if not n.startswith('_')])
for name in dir(wm):
    if name.startswith('_'): continue
    obj = getattr(wm, name)
    if inspect.isclass(obj):
        try: log(f"  class {name} sig:", inspect.signature(obj.__init__))
        except Exception as e: log(f"  class {name} sig err {e}")
print("\n".join(out))
with open("probe/probe1.txt","w") as f: f.write("\n".join(out))
