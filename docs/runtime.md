# How domainkit runs async work

What keeps the event loop free: which loop runs, how logs and error reports
leave it, how blocking code is found before production, and which servers
and Python builds domainkit runs on.

## Event loops: uvloop

`runserver` and every management command with an `async` handle
(`runworker`, `migrate`, `seed`, ...) run on [uvloop](https://github.com/MagicStack/uvloop)
when it is installed: `pip install 'domainkit[uvloop]'` (uvicorn's
`standard` extra brings it too, on Linux and macOS). `EVENT_LOOP` chooses:

| `EVENT_LOOP` | Loop |
| --- | --- |
| `"auto"` (default) | uvloop when it can be imported, asyncio's own otherwise |
| `"asyncio"` | always asyncio's own: rule uvloop out when chasing a bug |
| `"uvloop"` | uvloop, or a configuration error when it is missing |

Production servers pick their own loop: uvicorn and Granian take
`--loop uvloop` (both default to `auto`). Code that starts a loop of its own
can follow the setting:

```python
import asyncio

from domainkit.conf import settings
from domainkit.utils.loops import loop_factory

asyncio.run(main(), loop_factory=loop_factory(settings.EVENT_LOOP))
```

uvloop does not run on Windows; `auto` falls back to asyncio's loop there,
and the extra installs nothing.
