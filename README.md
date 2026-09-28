# clio
Perforce wrapper for easier version control of art assets.

Clio gives artists and tools a simple way to get, lock, save and branch
assets stored in Perforce. It is built around a C++ core (`clio_core`, on
the Perforce C++ API), used both from Python (`deda.clio`) and by a USD
asset resolver plugin that fetches `clio:` assets before USD loads them.

* [Design document](docs/design.md)
* [Building and testing](docs/building.md)

Targets: Python 3.13, USD 26.08 (and 25.08 or later).
