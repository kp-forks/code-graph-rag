"""
Refactored GraphUpdater with modular architecture.

This is the new modular version that maintains all functionality while
splitting the monolithic class into logical components.
"""

import os
from collections import defaultdict
from collections.abc import ItemsView, KeysView
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node, Parser

from .config import IGNORE_PATTERNS
from .language_config import get_language_config
from .parsers.factory import ProcessorFactory
from .services.graph_service import MemgraphIngestor


class FunctionRegistryTrie:
    """Trie data structure optimized for function qualified name lookups."""

    def __init__(self) -> None:
        self.root: dict[str, Any] = {}
        self._entries: dict[str, str] = {}

    def insert(self, qualified_name: str, func_type: str) -> None:
        """Insert a function into the trie."""
        self._entries[qualified_name] = func_type

        # Build trie path from qualified name parts
        parts = qualified_name.split(".")
        current = self.root

        for part in parts:
            if part not in current:
                current[part] = {}
            current = current[part]

        # Mark end of qualified name
        current["__type__"] = func_type
        current["__qn__"] = qualified_name

    def get(self, qualified_name: str, default: str | None = None) -> str | None:
        """Get function type by exact qualified name."""
        return self._entries.get(qualified_name, default)

    def __contains__(self, qualified_name: str) -> bool:
        """Check if qualified name exists in registry."""
        return qualified_name in self._entries

    def __getitem__(self, qualified_name: str) -> str:
        """Get function type by qualified name."""
        return self._entries[qualified_name]

    def __setitem__(self, qualified_name: str, func_type: str) -> None:
        """Set function type for qualified name."""
        self.insert(qualified_name, func_type)

    def __delitem__(self, qualified_name: str) -> None:
        """Remove qualified name from registry.

        Note: This only removes the entry from the dictionary-like interface for performance
        and simplicity. The node is not removed from the underlying trie structure, which
        may lead to memory growth in long-running sessions with many file deletions.
        """
        if qualified_name in self._entries:
            del self._entries[qualified_name]

    def keys(self) -> KeysView[str]:
        """Return all qualified names."""
        return self._entries.keys()

    def items(self) -> ItemsView[str, str]:
        """Return all (qualified_name, type) pairs."""
        return self._entries.items()

    def __len__(self) -> int:
        """Return number of entries."""
        return len(self._entries)

    def find_with_prefix_and_suffix(self, prefix: str, suffix: str) -> list[str]:
        """Find all qualified names that start with prefix and end with suffix."""
        results = []
        prefix_parts = prefix.split(".") if prefix else []

        # Navigate to prefix in trie
        current = self.root
        for part in prefix_parts:
            if part not in current:
                return []  # Prefix doesn't exist
            current = current[part]

        # DFS to find all entries under this prefix that end with suffix
        def dfs(node: dict[str, Any]) -> None:
            if "__qn__" in node:
                qn = node["__qn__"]
                if qn.endswith(f".{suffix}"):
                    results.append(qn)

            for key, child in node.items():
                if not key.startswith("__"):  # Skip metadata keys
                    dfs(child)

        dfs(current)
        return results

    def find_ending_with(self, suffix: str) -> list[str]:
        """Find all qualified names ending with the given suffix."""
        return [qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]


class GraphUpdater:
    """Parses code using Tree-sitter and updates the graph."""

    def __init__(
        self,
        ingestor: MemgraphIngestor,
        repo_path: Path,
        parsers: dict[str, Parser],
        queries: dict[str, Any],
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = self._prepare_queries_with_parsers(queries, parsers)
        self.project_name = repo_path.name
        self.function_registry = FunctionRegistryTrie()
        self.simple_name_lookup: dict[str, set[str]] = defaultdict(set)
        self.ast_cache: dict[Path, tuple[Node, str]] = {}
        self.ignore_dirs = IGNORE_PATTERNS

        # Create processor factory with all dependencies
        self.factory = ProcessorFactory(
            ingestor=self.ingestor,
            repo_path_getter=lambda: self.repo_path,
            project_name_getter=lambda: self.project_name,
            queries=self.queries,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            ast_cache=self.ast_cache,
        )

    def _prepare_queries_with_parsers(
        self, queries: dict[str, Any], parsers: dict[str, Parser]
    ) -> dict[str, Any]:
        """Add parser references to query objects for processors."""
        updated_queries = {}
        for lang, query_data in queries.items():
            if lang in parsers:
                updated_queries[lang] = {**query_data, "parser": parsers[lang]}
            else:
                updated_queries[lang] = query_data
        return updated_queries

    def run(self) -> None:
        """Orchestrates the parsing and ingestion process."""
        self.ingestor.ensure_node_batch("Project", {"name": self.project_name})
        logger.info(f"Ensuring Project: {self.project_name}")

        logger.info("--- Pass 1: Identifying Packages and Folders ---")
        self.factory.structure_processor.identify_structure()

        logger.info(
            "\n--- Pass 2: Processing Files, Caching ASTs, and Collecting Definitions ---"
        )
        self._process_files()

        logger.info(
            f"\n--- Found {len(self.function_registry)} functions/methods in codebase ---"
        )
        logger.info("--- Pass 3: Processing Function Calls from AST Cache ---")
        self._process_function_calls()

        # Process method overrides after all definitions are collected
        self.factory.definition_processor.process_all_method_overrides()

        logger.info("\n--- Analysis complete. Flushing all data to database... ---")
        self.ingestor.flush_all()

    def remove_file_from_state(self, file_path: Path) -> None:
        """Removes all state associated with a file from the updater's memory."""
        logger.debug(f"Removing in-memory state for: {file_path}")

        # Clear AST cache
        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug("  - Removed from ast_cache")

        # Determine the module qualified name prefix for the file
        relative_path = file_path.relative_to(self.repo_path)
        if file_path.name == "__init__.py":
            module_qn_prefix = ".".join(
                [self.project_name] + list(relative_path.parent.parts)
            )
        else:
            module_qn_prefix = ".".join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )

        # We need to find all qualified names that belong to this file/module
        qns_to_remove = set()

        # Clean function_registry and collect qualified names to remove
        for qn in list(self.function_registry.keys()):
            if qn.startswith(module_qn_prefix + ".") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(
                f"  - Removing {len(qns_to_remove)} QNs from function_registry"
            )

        # Clean simple_name_lookup
        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(f"  - Cleaned simple_name '{simple_name}'")

    def _process_files(self) -> None:
        """Second pass: Walks the directory, parses files, and caches their ASTs."""
        for root_str, dirs, files in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)

            for file_name in files:
                filepath = root / file_name

                # Check if this file type is supported for parsing
                lang_config = get_language_config(filepath.suffix)
                if lang_config and lang_config.name in self.parsers:
                    # Parse as Module and cache AST
                    result = self.factory.definition_processor.process_file(
                        filepath,
                        lang_config.name,
                        self.queries,
                        self.factory.structure_processor.structural_elements,
                    )
                    if result:
                        root_node, language = result
                        self.ast_cache[filepath] = (root_node, language)

                elif file_name == "pyproject.toml":
                    self.factory.definition_processor.process_dependencies(filepath)
                else:
                    # Use StructureProcessor to handle generic files
                    self.factory.structure_processor.process_generic_file(
                        filepath, file_name
                    )

    def _process_function_calls(self) -> None:
        """Third pass: Process function calls using the cached ASTs."""
        for file_path, (root_node, language) in self.ast_cache.items():
            self.factory.call_processor.process_calls_in_file(
                file_path, root_node, language, self.queries
            )
