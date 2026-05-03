#!/usr/bin/env python3
"""
Snippet Manager — CLI менеджер сниппетов кода.

Оптимизированная версия с:
- Индексы, FTS5 unicode61, датаклассы
- Пакетная вставка тегов, транзакции
- История поиска, сортировка
"""

import sqlite3
import os
import sys
import re
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from contextlib import contextmanager


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

DB_PATH: str = os.getenv("SNIPPET_DB", str(Path(__file__).parent / "snippets.db"))
HISTORY_LIMIT: int = 50


# ============================================================
# SQL ЗАПРОСЫ (КОНСТАНТЫ)
# ============================================================

SQL_TABLES: str = """
    CREATE TABLE IF NOT EXISTS languages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    );

    CREATE TABLE IF NOT EXISTS snippets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        code TEXT NOT NULL,
        language_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (language_id) REFERENCES languages(id)
    );

    CREATE TABLE IF NOT EXISTS snippet_tags (
        snippet_id INTEGER NOT NULL,
        tag_id INTEGER NOT NULL,
        PRIMARY KEY (snippet_id, tag_id),
        FOREIGN KEY (snippet_id) REFERENCES snippets(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags(id)
    );

    CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        result_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_snippets_lang ON snippets(language_id);
    CREATE INDEX IF NOT EXISTS idx_snippet_tags_tag ON snippet_tags(tag_id);
    CREATE INDEX IF NOT EXISTS idx_search_history_created ON search_history(created_at);
"""

SQL_FTS: str = """
    CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(
        title, description, code,
        tokenize='unicode61',
        content='snippets',
        content_rowid='id'
    );

    CREATE TRIGGER IF NOT EXISTS snippets_ai AFTER INSERT ON snippets BEGIN
        INSERT INTO snippets_fts(rowid, title, description, code)
        VALUES (new.id, new.title, new.description, new.code);
    END;

    CREATE TRIGGER IF NOT EXISTS snippets_ad AFTER DELETE ON snippets BEGIN
        INSERT INTO snippets_fts(snippets_fts, rowid, title, description, code)
        VALUES('delete', old.id, old.title, old.description, old.code);
    END;

    CREATE TRIGGER IF NOT EXISTS snippets_au AFTER UPDATE ON snippets BEGIN
        INSERT INTO snippets_fts(snippets_fts, rowid, title, description, code)
        VALUES('delete', old.id, old.title, old.description, old.code);
        INSERT INTO snippets_fts(rowid, title, description, code)
        VALUES (new.id, new.title, new.description, new.code);
    END;
"""

SQL_ADD_SNIPPET: str = """
    INSERT INTO snippets(title, description, code, language_id)
    VALUES (:title, :description, :code, :lang_id)
"""

SQL_GET_LANGUAGE_ID: str = "SELECT id FROM languages WHERE name = :name"

SQL_ADD_LANGUAGE: str = "INSERT OR IGNORE INTO languages(name) VALUES (:name)"

SQL_GET_TAG_IDS: str = "SELECT id, name FROM tags WHERE name IN ({})"

SQL_ADD_TAG: str = "INSERT OR IGNORE INTO tags(name) VALUES (:name)"

SQL_LINK_TAG: str = "INSERT OR IGNORE INTO snippet_tags(snippet_id, tag_id) VALUES (:snippet_id, :tag_id)"

SQL_SEARCH_FTS: str = """
    SELECT s.*, l.name as language_name,
           GROUP_CONCAT(DISTINCT t.name, ', ') as tags
    FROM snippets_fts fts
    JOIN snippets s ON s.id = fts.rowid
    LEFT JOIN languages l ON s.language_id = l.id
    LEFT JOIN snippet_tags st ON s.id = st.snippet_id
    LEFT JOIN tags t ON st.tag_id = t.id
    WHERE snippets_fts MATCH :query
    GROUP BY s.id
    ORDER BY rank
    LIMIT :limit
"""

SQL_BY_TAG: str = """
    SELECT s.*, l.name as language_name,
           GROUP_CONCAT(DISTINCT t.name, ', ') as tags
    FROM snippets s
    JOIN snippet_tags st ON s.id = st.snippet_id
    JOIN tags t ON st.tag_id = t.id
    LEFT JOIN languages l ON s.language_id = l.id
    WHERE t.name = :tag
    GROUP BY s.id
    ORDER BY s.updated_at DESC
    LIMIT :limit
"""

SQL_BY_LANGUAGE: str = """
    SELECT s.*, l.name as language_name,
           GROUP_CONCAT(DISTINCT t.name, ', ') as tags
    FROM snippets s
    JOIN languages l ON s.language_id = l.id
    LEFT JOIN snippet_tags st ON s.id = st.snippet_id
    LEFT JOIN tags t ON st.tag_id = t.id
    WHERE l.name = :lang
    GROUP BY s.id
    ORDER BY s.updated_at DESC
    LIMIT :limit
"""

SQL_LIST_ALL: str = """
    SELECT s.*, l.name as language_name,
           GROUP_CONCAT(DISTINCT t.name, ', ') as tags
    FROM snippets s
    LEFT JOIN languages l ON s.language_id = l.id
    LEFT JOIN snippet_tags st ON s.id = st.snippet_id
    LEFT JOIN tags t ON st.tag_id = t.id
    GROUP BY s.id
    ORDER BY {}
    LIMIT :limit
"""

SQL_GET_BY_ID: str = """
    SELECT s.*, l.name as language_name,
           GROUP_CONCAT(DISTINCT t.name, ', ') as tags
    FROM snippets s
    LEFT JOIN languages l ON s.language_id = l.id
    LEFT JOIN snippet_tags st ON s.id = st.snippet_id
    LEFT JOIN tags t ON st.tag_id = t.id
    WHERE s.id = :id
    GROUP BY s.id
"""

SQL_DELETE_SNIPPET: str = "DELETE FROM snippets WHERE id = :id"

SQL_ADD_HISTORY: str = """
    INSERT INTO search_history(query, result_count) VALUES (:query, :count)
"""

SQL_GET_HISTORY: str = """
    SELECT * FROM search_history ORDER BY created_at DESC LIMIT :limit
"""

SQL_GET_HISTORY_TOP: str = """
    SELECT query, COUNT(*) as cnt, MAX(created_at) as last_used
    FROM search_history
    GROUP BY query
    ORDER BY cnt DESC
    LIMIT :limit
"""

SQL_CLEAR_HISTORY: str = "DELETE FROM search_history"

SQL_CLEANUP_HISTORY: str = """
    DELETE FROM search_history
    WHERE id NOT IN (
        SELECT id FROM search_history
        ORDER BY created_at DESC
        LIMIT :limit
    )
"""


# ============================================================
# МОДЕЛИ ДАННЫХ
# ============================================================

@dataclass
class Snippet:
    """Модель сниппета."""
    id: int
    title: str
    description: str
    code: str
    language: str | None = None
    tags: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        """Преобразует language_name в language если нужно."""
        if self.language is None and hasattr(self, 'language_name'):
            self.language = self.language_name

    @property
    def tag_list(self) -> list[str]:
        """Возвращает список отдельных тегов."""
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(',') if t.strip()]


@dataclass
class SearchHistoryItem:
    """Модель элемента истории поиска."""
    id: int
    query: str
    result_count: int
    created_at: str


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

class Database:
    """Класс для работы с базой данных."""

    @staticmethod
    @contextmanager
    def connection() -> sqlite3.Connection:
        """
        Контекстный менеджер для соединения с БД.

        Yields:
            sqlite3.Connection: Активное соединение с БД.
        """
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def initialize() -> None:
        """
        Инициализирует все таблицы, индексы и FTS5.
        """
        with Database.connection() as conn:
            conn.executescript(SQL_TABLES)
            conn.executescript(SQL_FTS)
            conn.commit()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def is_valid_fts_query(query: str) -> bool:
    """
    Проверяет валидность FTS5 запроса.

    Args:
        query: Поисковый запрос.

    Returns:
        bool: True если запрос валиден.
    """
    if not query or not query.strip():
        return False
    return not re.match(r'^[!"$()*+\-.:<=>?@\[\]^`{|}~]', query.strip())


def read_multiline() -> str:
    """
    Читает многострочный текст с CLI.

    Returns:
        str: Введённый текст.
    """
    print("Введите код (Enter на пустой строке для завершения):")
    lines = []
    while True:
        try:
            line = input()
            if line.strip() == '':
                break
            lines.append(line)
        except (EOFError, KeyboardInterrupt):
            break
    return '\n'.join(lines)


def format_sort_column(sort_by: str, ascending: bool) -> str:
    """
    Формирует SQL выражение для сортировки.

    Args:
        sort_by: Поле для сортировки (date, title, lang).
        ascending: По возрастанию или убыванию.

    Returns:
        str: SQL выражение ORDER BY.
    """
    direction = "ASC" if ascending else "DESC"

    sort_mapping = {
        "date": "s.created_at",
        "created_at": "s.created_at",
        "title": "s.title",
        "lang": "l.name",
        "language": "l.name",
    }

    column = sort_mapping.get(sort_by, "s.created_at")
    return f"{column} {direction}"


# ============================================================
# МЕНЕДЖЕР СНИППЕТОВ (CRUD)
# ============================================================

class SnippetManager:
    """Класс для управления сниппетами."""

    @staticmethod
    def add_snippet(
        title: str,
        code: str,
        description: str = "",
        language: str | None = None,
        tags: list[str] | None = None
    ) -> int:
        """
        Добавляет новый сниппет.

        Args:
            title: Название сниппета.
            code: Код сниппета.
            description: Описание (опционально).
            language: Язык программирования (опционально).
            tags: Список тегов (опционально).

        Returns:
            int: ID созданного сниппета.
        """
        with Database.connection() as conn:
            conn.execute("BEGIN")
            cursor = conn.cursor()

            lang_id: int | None = None
            if language:
                lang = language.lower().strip()
                cursor.execute(SQL_ADD_LANGUAGE, {"name": lang})
                cursor.execute(SQL_GET_LANGUAGE_ID, {"name": lang})
                row = cursor.fetchone()
                lang_id = row[0] if row else None

            cursor.execute(SQL_ADD_SNIPPET, {
                "title": title,
                "description": description,
                "code": code,
                "lang_id": lang_id
            })
            snippet_id = cursor.lastrowid

            if tags:
                clean_tags = [t.strip().lower() for t in tags if t.strip()]
                if clean_tags:
                    cursor.executemany(
                        SQL_ADD_TAG,
                        [{"name": t} for t in clean_tags]
                    )

                    placeholders = ','.join(['?'] * len(clean_tags))
                    cursor.execute(
                        f"SELECT id, name FROM tags WHERE name IN ({placeholders})",
                        clean_tags
                    )
                    tag_map = {row['name']: row['id'] for row in cursor.fetchall()}

                    snippet_tag_pairs = [
                        (snippet_id, tag_map[t])
                        for t in clean_tags if t in tag_map
                    ]
                    cursor.executemany(SQL_LINK_TAG, snippet_tag_pairs)

            conn.commit()
            return snippet_id

    @staticmethod
    def search(
        query: str,
        limit: int = 20,
        sort_by: str = "date",
        ascending: bool = False
    ) -> list[Snippet]:
        """
        Выполняет полнотекстовый поиск.

        Args:
            query: Поисковый запрос.
            limit: Максимум результатов.
            sort_by: Поле сортировки.
            ascending: По возрастанию?

        Returns:
            list[Snippet]: Найденные сниппеты.
        """
        if not is_valid_fts_query(query):
            return []

        with Database.connection() as conn:
            try:
                sort_expr = format_sort_column(sort_by, ascending)
                sql = SQL_LIST_ALL.format(sort_expr)

                rows = conn.execute(sql, {"limit": limit}).fetchall()

                results = [Snippet(**dict(row)) for row in rows]
                filtered = [s for s in results if query.lower() in s.title.lower()
                           or query.lower() in s.description.lower()
                           or query.lower() in s.code.lower()]

                if filtered:
                    Database._save_history(query, len(filtered))

                return filtered[:limit]

            except sqlite3.OperationalError:
                return []

    @staticmethod
    def _save_history(query: str, result_count: int) -> None:
        """
        Сохраняет запрос в историю.

        Args:
            query: Поисковый запрос.
            result_count: Количество результатов.
        """
        with Database.connection() as conn:
            conn.execute(SQL_ADD_HISTORY, {
                "query": query,
                "count": result_count
            })
            conn.execute(SQL_CLEANUP_HISTORY, {"limit": HISTORY_LIMIT})
            conn.commit()

    @staticmethod
    def get_by_tag(tag_name: str, limit: int = 50) -> list[Snippet]:
        """
        Ищет сниппеты по тегу.

        Args:
            tag_name: Название тега.
            limit: Максимум результатов.

        Returns:
            list[Snippet]: Найденные сниппеты.
        """
        with Database.connection() as conn:
            rows = conn.execute(SQL_BY_TAG, {
                "tag": tag_name.lower().strip(),
                "limit": limit
            }).fetchall()
            return [Snippet(**dict(row)) for row in rows]

    @staticmethod
    def get_by_language(lang_name: str, limit: int = 50) -> list[Snippet]:
        """
        Ищет сниппеты по языку.

        Args:
            lang_name: Язык программирования.
            limit: Максимум результатов.

        Returns:
            list[Snippet]: Найденные сниппеты.
        """
        with Database.connection() as conn:
            rows = conn.execute(SQL_BY_LANGUAGE, {
                "lang": lang_name.lower().strip(),
                "limit": limit
            }).fetchall()
            return [Snippet(**dict(row)) for row in rows]

    @staticmethod
    def list_all(
        limit: int = 50,
        sort_by: str = "date",
        ascending: bool = False
    ) -> list[Snippet]:
        """
        Список всех сниппетов.

        Args:
            limit: Максимум результатов.
            sort_by: Поле сортировки.
            ascending: По возрастанию?

        Returns:
            list[Snippet]: Все сниппеты.
        """
        with Database.connection() as conn:
            sort_expr = format_sort_column(sort_by, ascending)
            sql = SQL_LIST_ALL.format(sort_expr)

            rows = conn.execute(sql, {"limit": limit}).fetchall()
            return [Snippet(**dict(row)) for row in rows]

    @staticmethod
    def get_snippet(snippet_id: int) -> Snippet | None:
        """
        Получает сниппет по ID.

        Args:
            snippet_id: ID сниппета.

        Returns:
            Snippet | None: Найденный сниппет или None.
        """
        with Database.connection() as conn:
            row = conn.execute(SQL_GET_BY_ID, {"id": snippet_id}).fetchone()
            if row:
                return Snippet(**dict(row))
            return None

    @staticmethod
    def delete_snippet(snippet_id: int) -> bool:
        """
        Удаляет сниппет.

        Args:
            snippet_id: ID сниппета.

        Returns:
            bool: True если удалён.
        """
        with Database.connection() as conn:
            cursor = conn.execute(SQL_DELETE_SNIPPET, {"id": snippet_id})
            conn.commit()
            return cursor.rowcount > 0


# ============================================================
# ИСТОРИЯ ПОИСКА
# ============================================================

class SearchHistory:
    """Класс для управления историей поиска."""

    @staticmethod
    def get_all(limit: int = 50) -> list[SearchHistoryItem]:
        """
        Получает историю поиска.

        Args:
            limit: Максимум записей.

        Returns:
            list[SearchHistoryItem]: История поиска.
        """
        with Database.connection() as conn:
            rows = conn.execute(SQL_GET_HISTORY, {"limit": limit}).fetchall()
            return [SearchHistoryItem(**dict(row)) for row in rows]

    @staticmethod
    def get_top(limit: int = 10) -> list[dict]:
        """
        Получает топ популярных запросов.

        Args:
            limit: Количество топ запросов.

        Returns:
            list[dict]: Топ запросов.
        """
        with Database.connection() as conn:
            rows = conn.execute(SQL_GET_HISTORY_TOP, {"limit": limit}).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def clear() -> None:
        """Очищает историю поиска."""
        with Database.connection() as conn:
            conn.execute(SQL_CLEAR_HISTORY)
            conn.commit()


# ============================================================
# CLI КОМАН��Ы
# ============================================================

def handle_add(args: argparse.Namespace) -> None:
    """Добавляет новый сниппет."""
    code = read_multiline()
    if not code.strip():
        print("Ошибка: код не может быть пустым")
        return

    tags_list = [t.strip() for t in args.tags.split(',')] if args.tags else None

    try:
        snippet_id = SnippetManager.add_snippet(
            title=args.title,
            code=code,
            description=args.description or "",
            language=args.language,
            tags=tags_list
        )
        print(f"Сниппет создан! ID: {snippet_id}")
    except Exception as e:
        print(f"Ошибка: {e}")


def handle_search(args: argparse.Namespace) -> None:
    """Полнотекстовый поиск."""
    results = SnippetManager.search(
        query=args.query,
        limit=args.limit,
        sort_by=args.sort,
        ascending=args.asc
    )

    if not results:
        print("Ничего не найдено")
        return

    print(f"\nРезультаты поиска '{args.query}': {len(results)} найдено\n")
    for s in results:
        print(f"[{s.id}] {s.title}")
        print(f"    Язык: {s.language or 'не указан'}")
        if s.tags:
            print(f"    Теги: {s.tags}")
        if s.description:
            print(f"    {s.description[:100]}")
        print()


def handle_show(args: argparse.Namespace) -> None:
    """Показывает сниппет."""
    snippet = SnippetManager.get_snippet(args.id)
    if not snippet:
        print(f"Сниппет с ID {args.id} не найден")
        return

    print(f"\n{'='*60}")
    print(f"ID: {snippet.id} | {snippet.title}")
    print(f"{'='*60}")
    print(f"Язык:  {snippet.language or 'не указан'}")
    print(f"Теги:  {snippet.tags or 'нет'}")
    print(f"Создан: {snippet.created_at}")
    if snippet.description:
        print(f"Описание: {snippet.description}")
    print(f"\n--- Код ---\n")
    print(snippet.code)
    print(f"\n{'='*60}\n")


def handle_list(args: argparse.Namespace) -> None:
    """List all snippets."""
    snippets = SnippetManager.list_all(
        limit=args.limit,
        sort_by=args.sort,
        ascending=args.asc
    )

    if not snippets:
        print("No snippets yet")
        return

    count = len(snippets)
    print(f"\nSnippets ({count}):\n")
    print(f"{'ID':<5} {'Title':<35} {'Lang':<12} {'Date':<10}")
    print("-" * 65)
    for s in snippets:
        title = s.title[:33] + '..' if len(s.title) > 35 else s.title
        lang = s.language or '--'
        date = s.created_at[:10] if s.created_at else '--'
        print(f"{s.id:<5} {title:<35} {lang:<12} {date:<10}")


def handle_tag(args: argparse.Namespace) -> None:
    """Поиск по тегу."""
    results = SnippetManager.get_by_tag(args.tag)
    if not results:
        print(f"Нет сниппетов с тегом '{args.tag}'")
        return

    print(f"\nСниппеты с тегом '{args.tag}': {len(results)}\n")
    for s in results:
        print(f"[{s.id}] {s.title} ({s.language or '--'})")


def handle_lang(args: argparse.Namespace) -> None:
    """Поиск по языку."""
    results = SnippetManager.get_by_language(args.language)
    if not results:
        print(f"Нет сниппетов на языке '{args.language}'")
        return

    print(f"\nСниппеты на '{args.language}': {len(results)}\n")
    for s in results:
        print(f"[{s.id}] {s.title}")


def handle_delete(args: argparse.Namespace) -> None:
    """Удаляет сниппет."""
    snippet = SnippetManager.get_snippet(args.id)
    if not snippet:
        print(f"Сниппет с ID {args.id} не найден")
        return

    confirm = input(f"Удалить сниппет '{snippet.title}' (ID: {args.id})? [y/N]: ")
    if confirm.lower() == 'y':
        if SnippetManager.delete_snippet(args.id):
            print("Сниппет удалён")
        else:
            print("Не удалось удалить")
    else:
        print("Отменено")


def handle_history(args: argparse.Namespace) -> None:
    """Управляет историей поиска."""
    if args.clear:
        SearchHistory.clear()
        print("История очищена")
        return

    if args.top:
        items = SearchHistory.get_top(args.top)
        print(f"\nТоп запросов:\n")
        print(f"{'Запрос':<40} {'Раз':<6} {'Последний':<20}")
        print("-" * 70)
        for item in items:
            query = item['query'][:38] + '..' if len(item['query']) > 40 else item['query']
            print(f"{query:<40} {item['cnt']:<6} {item['last_used'][:19]:<20}")
        return

    items = SearchHistory.get_all()
    if not items:
        print("История пуста")
        return

    print(f"\nИстория поиска ({len(items)}):\n")
    print(f"{'ID':<5} {'Запрос':<40} {'Результатов':<10} {'Дата':<20}")
    print("-" * 78)
    for item in items:
        query = item.query[:38] + '..' if len(item.query) > 40 else item.query
        print(f"{item.id:<5} {query:<40} {item.result_count:<10} {item.created_at[:19]:<20}")


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

def main() -> None:
    """Точка входа в программу."""
    Database.initialize()

    parser = argparse.ArgumentParser(
        description="Snippet Manager — менеджер сниппетов кода",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python snippet_manager.py add -t "Чтение CSV" -l python -g "csv,файлы"
  python snippet_manager.py search "csv" -l 10 --sort date --desc
  python snippet_manager.py list --sort title --asc
  python snippet_manager.py history
  python snippet_manager.py history --top 5
  python snippet_manager.py history --clear
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Команды")

    # add
    p_add = subparsers.add_parser("add", help="Добавить сниппет")
    p_add.add_argument("-t", "--title", required=True, help="Название")
    p_add.add_argument("-d", "--description", default="", help="Описание")
    p_add.add_argument("-l", "--language", help="Язык")
    p_add.add_argument("-g", "--tags", help="Теги через запятую")
    p_add.set_defaults(func=handle_add)

    # search
    p_search = subparsers.add_parser("search", help="Поиск")
    p_search.add_argument("query", help="Запрос")
    p_search.add_argument("-l", "--limit", type=int, default=20, help="Лимит")
    p_search.add_argument("-s", "--sort", default="date",
                         choices=["date", "title", "lang"], help="Сортировка")
    p_search.add_argument("--asc", action="store_true", help="По возрастанию")
    p_search.add_argument("--desc", action="store_true", help="По убыванию")
    p_search.set_defaults(func=handle_search, asc=False)

    # show
    p_show = subparsers.add_parser("show", help="Показать сниппет")
    p_show.add_argument("id", type=int, help="ID сниппета")
    p_show.set_defaults(func=handle_show)

    # list
    p_list = subparsers.add_parser("list", help="Список сниппетов")
    p_list.add_argument("-l", "--limit", type=int, default=50, help="Лимит")
    p_list.add_argument("-s", "--sort", default="date",
                     choices=["date", "title", "lang"], help="Сортировка")
    p_list.add_argument("--asc", action="store_true", help="По возрастанию")
    p_list.add_argument("--desc", action="store_true", help="По убыванию")
    p_list.set_defaults(func=handle_list, asc=False)

    # tag
    p_tag = subparsers.add_parser("tag", help="Поиск по тегу")
    p_tag.add_argument("tag", help="Тег")
    p_tag.set_defaults(func=handle_tag)

    # lang
    p_lang = subparsers.add_parser("lang", help="Поиск по языку")
    p_lang.add_argument("language", help="Язык")
    p_lang.set_defaults(func=handle_lang)

    # delete
    p_del = subparsers.add_parser("delete", help="Удалить сниппет")
    p_del.add_argument("id", type=int, help="ID сниппета")
    p_del.set_defaults(func=handle_delete)

    # history
    p_hist = subparsers.add_parser("history", help="История поиска")
    p_hist.add_argument("--clear", action="store_true", help="Очистить историю")
    p_hist.add_argument("--top", type=int, default=5, help="Топ N запросов")
    p_hist.set_defaults(func=handle_history)

    args = parser.parse_args()

    if hasattr(args, "func"):
        try:
            args.func(args)
        except KeyboardInterrupt:
            print("\nПрервано")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
