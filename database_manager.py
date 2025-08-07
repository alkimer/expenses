# database_manager.py

import sqlite3
import datetime
from typing import List, Tuple
from transaction import Transaction

class DatabaseManager:
    def __init__(self, db_path: str = 'expenses.db') -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute('PRAGMA foreign_keys=ON;')
        self._create_tables()
        self._ensure_default_category()
        self._ensure_cash_statement()

    def _create_tables(self) -> None:
        c = self.conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stores(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                category_id INTEGER,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );
            CREATE TABLE IF NOT EXISTS statements(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month INTEGER NOT NULL,
                year INTEGER NOT NULL,
                card_name TEXT NOT NULL,
                last4digits TEXT NOT NULL,
                file_path TEXT,
                UNIQUE(month,year,card_name,last4digits)
            );
            CREATE TABLE IF NOT EXISTS transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                store_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                installment_number INTEGER,
                FOREIGN KEY(statement_id) REFERENCES statements(id) ON DELETE CASCADE,
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );
        """)
        self.conn.commit()

    def _ensure_default_category(self) -> None:
        c = self.conn.cursor()
        c.execute("SELECT 1 FROM categories WHERE name=?", ("NO ASIGNADA",))
        if not c.fetchone():
            c.execute("INSERT INTO categories(name) VALUES(?)", ("NO ASIGNADA",))
            self.conn.commit()

    def _ensure_cash_statement(self) -> None:
        c = self.conn.cursor()
        c.execute(
            "SELECT 1 FROM statements WHERE year=? AND month=? AND card_name=? AND last4digits=?",
            (1900, 1, "EFECTIVO", "")
        )
        if not c.fetchone():
            c.execute(
                "INSERT INTO statements(month,year,card_name,last4digits,file_path) VALUES(?,?,?,?,NULL)",
                (1, 1900, "EFECTIVO", "")
            )
            self.conn.commit()

    def get_cash_statement_id(self) -> int:
        c = self.conn.cursor()
        c.execute(
            "SELECT id FROM statements WHERE year=? AND month=? AND card_name=? AND last4digits=?",
            (1900, 1, "EFECTIVO", "")
        )
        return c.fetchone()[0]

    def get_default_category_id(self) -> int:
        c = self.conn.cursor()
        c.execute("SELECT id FROM categories WHERE name=?", ("NO ASIGNADA",))
        return c.fetchone()[0]

    # Category operations
    def add_category(self, name: str) -> None:
        c = self.conn.cursor()
        try:
            c.execute("INSERT INTO categories(name) VALUES(?)", (name,))
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"La categoría '{name}' ya existe.")

    def delete_category(self, cid: int) -> None:
        default = self.get_default_category_id()
        if cid == default:
            raise ValueError("No se puede eliminar la categoría por defecto.")
        c = self.conn.cursor()
        c.execute("UPDATE stores SET category_id=? WHERE category_id=?", (default, cid))
        c.execute("DELETE FROM categories WHERE id=?", (cid,))
        self.conn.commit()

    def get_categories(self) -> List[Tuple[int, str]]:
        c = self.conn.cursor()
        c.execute("SELECT id, name FROM categories ORDER BY name ASC")
        return c.fetchall()

    # Store operations
    def get_store_id(self, name: str) -> int:
        c = self.conn.cursor()
        c.execute("SELECT id FROM stores WHERE name=?", (name,))
        row = c.fetchone()
        if row:
            return row[0]
        default = self.get_default_category_id()
        c.execute("INSERT INTO stores(name,category_id) VALUES(?,?)", (name, default))
        self.conn.commit()
        return c.lastrowid

    def update_store_category(self, sid: int, cid: int) -> None:
        c = self.conn.cursor()
        c.execute("UPDATE stores SET category_id=? WHERE id=?", (cid, sid))
        self.conn.commit()

    def get_store_category(self, sid: int) -> Tuple[int, str]:
        c = self.conn.cursor()
        c.execute(
            "SELECT c.id,c.name FROM stores s JOIN categories c ON s.category_id=c.id WHERE s.id=?",
            (sid,)
        )
        return c.fetchone() or (self.get_default_category_id(), "NO ASIGNADA")

    # Statement operations
    def add_statement(self, month: int, year: int, card_name: str, last4digits: str, file_path: str) -> int:
        c = self.conn.cursor()
        try:
            c.execute(
                "INSERT INTO statements(month,year,card_name,last4digits,file_path) VALUES(?,?,?,?,?)",
                (month, year, card_name, last4digits, file_path)
            )
            self.conn.commit()
            return c.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"Resumen ya existe para {month}/{year} de {card_name}.")

    def get_statements(self) -> List[Tuple[int, int, int, str, str]]:
        c = self.conn.cursor()
        c.execute(
            "SELECT id, month, year, card_name, last4digits FROM statements "
            "ORDER BY year DESC, month DESC, card_name"
        )
        return c.fetchall()

    # Transaction operations
    def add_transactions(self, sid: int, txs: List[Transaction]) -> None:
        c = self.conn.cursor()
        for t in txs:
            store_id = self.get_store_id(t.store_name)
            c.execute(
                "INSERT INTO transactions(statement_id,date,store_id,amount,installment_number) "
                "VALUES(?,?,?,?,?)",
                (sid, t.date.isoformat(), store_id, t.amount, t.installment_number)
            )
        self.conn.commit()

    def get_transactions_by_statement(self, sid: int) -> List[Tuple[int, str, str, float, int, int]]:
        c = self.conn.cursor()
        c.execute(
            "SELECT t.id,t.date,s.name,t.amount,t.installment_number,s.id "
            "FROM transactions t JOIN stores s ON t.store_id=s.id "
            "WHERE t.statement_id=? ORDER BY t.date ASC", (sid,)
        )
        return c.fetchall()

    def get_category_sums(self, sid: int) -> List[Tuple[str, float]]:
        c = self.conn.cursor()
        c.execute(
            "SELECT c.name,SUM(t.amount) FROM transactions t "
            "JOIN stores s ON t.store_id=s.id "
            "JOIN categories c ON s.category_id=c.id "
            "WHERE t.statement_id=? "
            "GROUP BY c.id ORDER BY SUM(t.amount) DESC", (sid,)
        )
        return c.fetchall()

    def get_all_category_sums(self) -> List[Tuple[str, float]]:
        c = self.conn.cursor()
        c.execute(
            "SELECT c.name,SUM(t.amount) FROM transactions t "
            "JOIN stores s ON t.store_id=s.id "
            "JOIN categories c ON s.category_id=c.id "
            "GROUP BY c.id ORDER BY SUM(t.amount) DESC"
        )
        return c.fetchall()

    def add_manual_transaction(self, sid: int, date: datetime.date, name: str, amt: float) -> None:
        c = self.conn.cursor()
        store_id = self.get_store_id(name)
        c.execute(
            "INSERT INTO transactions(statement_id,date,store_id,amount,installment_number) "
            "VALUES(?,?,?,?,NULL)",
            (sid, date.isoformat(), store_id, amt)
        )
        self.conn.commit()
