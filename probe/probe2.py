import inspect, traceback
out=[]
def log(*a): out.append(" ".join(str(x) for x in a))
from agentscope.app.storage._sql import AsyncSQLAlchemyStorage
log("AsyncSQLAlchemyStorage sig:", inspect.signature(AsyncSQLAlchemyStorage.__init__))
log("module:", AsyncSQLAlchemyStorage.__module__)
# deps
from agentscope.app import deps
log("deps exports:", [n for n in dir(deps) if not n.startswith('_')])
log("get_current_user_id sig:", inspect.signature(deps.get_current_user_id))
# routers
import agentscope.app._router as r, pkgutil
log("router pkg modules:", [m.name for m in pkgutil.iter_modules(r.__path__)])
# Agent class
from agentscope.agent import Agent
log("Agent sig:", inspect.signature(Agent.__init__))
# model base
from agentscope.model import ChatModelBase
log("ChatModelBase abstract methods:", getattr(ChatModelBase, '__abstractmethods__', None))
log("ChatModelBase sig:", inspect.signature(ChatModelBase.__init__))
print("\n".join(out))
open("probe/probe2.txt","w").write("\n".join(out))
