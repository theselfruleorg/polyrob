"""Mixin classes for MessageManager (pure code-motion split of service.py).

Concern-group mixins composed into MessageManager:
- TokenCounterMixin: token counting / limits / usage estimation
- CompactorMixin: history compaction and emergency pruning
- PersistenceMixin: checkpoint + disk save/load
- FiltersMixin: sensitive-data scrub, tool-sequence repair, message conversion
"""
