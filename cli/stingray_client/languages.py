"""Extension → language label for ticket code blocks.

Lives in the shared client package because both the CLI's diff collector
(``stingray_cli.gitctx``) and the stub scanner (``stingray_client.stubs``, which
the resolver also imports) need it, and neither should depend on the other.
"""
from __future__ import annotations

from pathlib import Path

# Extension → the language label stored on a code block (drives UI highlighting).
LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".scala": "scala",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
    ".sql": "sql", ".css": "css", ".scss": "scss", ".html": "html",
    ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".md": "markdown", ".rst": "rst", ".xml": "xml",
}


def language_for(path: str) -> str:
    return LANGUAGES.get(Path(path).suffix.lower(), "text")
