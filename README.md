# clio
Perforce wrapper for easier version control of art assets.

Clio gives artists and tools a simple way to get, lock, save and branch
assets stored in Perforce. It is built around a C++ core (`clio_core`, on
the Perforce C++ API), used both from Python (`deda.clio`) and by a USD
asset resolver plugin that fetches files from Perforce before USD loads them.
USD files keep ordinary paths, so they open with or without Clio.

```bash
clio setup --root D:/work/imagine --depot //imagine/main   # once
clio get props/crate
clio lock props/crate/crate.ma
clio save -m "Crate: damage pass on lid" props/crate
clio status
```

```python
from deda import clio

with clio.connect("imagine") as s:
    s.workspace.lock("props/crate/crate.ma")
    s.workspace.save("props/crate", message="Crate: damage pass on lid")
```

* [Everyday workflow: get, lock, save](docs/workflow.md) (CLI and Python)
* [Using Clio with USD](docs/usd.md)
* [Building and testing](docs/building.md)
* [Design document](docs/design.md) and [benchmarks](docs/benchmarks.md)

Targets: Python 3.13, USD 26.08 (and 25.08 or later).
