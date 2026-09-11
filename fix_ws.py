with open("venues/binance/public_ws.py", "r") as f:
    code = f.read()
code = code.replace("from typing import List, Dict, Any", "from typing import List, Dict, Any, Optional, Callable, Coroutine")
with open("venues/binance/public_ws.py", "w") as f:
    f.write(code)
