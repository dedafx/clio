"""View models for UIs (design doc §9.5).

They have no Qt dependency but map directly onto ``QAbstractItemModel`` (or
any other toolkit): ``__len__``, ``__getitem__`` and signals. They own
batching, prioritization and cancellation, so a UI never calls the server
per row and does not reinvent any of it.

>>> from deda import clio
>>> from deda.clio.views import FileListView, HistoryView
>>> s = clio.connect(project="imagine")
>>> files = FileListView(s, "props/crate")
>>> files.rows_changed.connect(on_rows_changed)   # (first, last)
>>> files.set_visible_range(0, 40)                # only these rows are asked for
>>> hist = HistoryView(s, "props/crate/crate.ma", page_size=20)
>>> hist.fetch_more()                             # older revisions, when the user scrolls

Signal slots run on a worker thread, never the UI thread. A UI marshals them
to its own (in Qt, a queued signal).
"""

from deda.clio.views._pipeline import RequestPipeline, Signal
from deda.clio.views.activity import ActivityRow, ActivityView
from deda.clio.views.branches import (
    BranchDiffRow,
    BranchDiffView,
    BranchListView,
    BranchRow,
    DiffKind,
    Relation,
)
from deda.clio.views.filelist import COLUMNS, FileListView, FileRow
from deda.clio.views.history import HistoryRow, HistoryView

__all__ = [
    "COLUMNS",
    "ActivityRow",
    "ActivityView",
    "BranchDiffRow",
    "BranchDiffView",
    "BranchListView",
    "BranchRow",
    "DiffKind",
    "FileListView",
    "FileRow",
    "HistoryRow",
    "HistoryView",
    "Relation",
    "RequestPipeline",
    "Signal",
]
