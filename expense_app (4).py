#!/usr/bin/env python3
"""
Expense Manager Application
==========================

This module implements a desktop application for tracking and categorising
credit-card expenses. It reads PDF statements from Visa and Mastercard,
extracts individual transactions, persists them in a SQLite database,
allows users to map merchant names to categories, and presents an
interactive view of spending, including summary charts.

Key Features
------------
* **Persistent database**: All data is stored in a SQLite database, allowing
  the application state to survive between runs.
* **PDF import**: Statements from different card providers are parsed
  automatically using heuristics to identify the date, merchant, amount and
  instalment number from each line in the PDF. Unsupported or malformed
  lines are skipped gracefully.
* **Category management**: Users can add or delete categories at any time.
  A default category (“NO ASIGNADA”) ensures that uncategorised expenses are
  clearly separated.
* **Merchant mapping**: The relationship between merchant names and
  categories is persisted. Updating a mapping immediately affects all
  existing transactions associated with that merchant.
* **Interactive UI**: The application presents statements and their
  transactions in a table, allows inline editing of merchant categories
  via dropdowns, and displays a pie chart of spending by category for the
  selected month.

Note
----
This application uses the Tkinter GUI toolkit. If you run this code in
an environment without a display (e.g. a headless server), the GUI will
not launch. Nevertheless, the core database and PDF parsing logic can still be
exercised programmatically.

"""

import os
import re
import sqlite3
import datetime
import pandas as pd
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Attempt to import GUI and plotting libraries. These modules may be
# unavailable in headless environments. We delay their import inside the
# classes that need them so that the non-GUI parts of the code can still be
# loaded and tested without requiring a display.
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False


@dataclass
class Transaction:
    """Represents a single transaction extracted from a statement."""
    date: datetime.date
    store_name: str
    amount: float
    installment_number: Optional[int] = None


class DatabaseManager:
    """Encapsulates all database interactions."""

    def __init__(self, db_path: str = 'expenses.db') -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute('PRAGMA foreign_keys = ON;')
        self._create_tables()
        self._ensure_default_category()

    def _create_tables(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                category_id INTEGER,
                FOREIGN KEY (category_id) REFERENCES categories(id)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month INTEGER NOT NULL,
                year INTEGER NOT NULL,
                card_name TEXT NOT NULL,
                last4digits TEXT NOT NULL,
                file_path TEXT,
                UNIQUE (month, year, card_name, last4digits)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                store_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                installment_number INTEGER,
                FOREIGN KEY (statement_id) REFERENCES statements(id) ON DELETE CASCADE,
                FOREIGN KEY (store_id) REFERENCES stores(id)
            );
            """
        )
        self.conn.commit()

    def _ensure_default_category(self) -> None:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM categories WHERE name = ?", ("NO ASIGNADA",))
        if cur.fetchone() is None:
            cur.execute("INSERT INTO categories (name) VALUES (?)", ("NO ASIGNADA",))
            self.conn.commit()

    def get_default_category_id(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM categories WHERE name = ?", ("NO ASIGNADA",))
        row = cur.fetchone()
        return row[0] if row else 1

    # Category operations
    def add_category(self, name: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute("INSERT INTO categories (name) VALUES (?)", (name,))
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"La categoría '{name}' ya existe.")

    def delete_category(self, category_id: int) -> None:
        default_id = self.get_default_category_id()
        if category_id == default_id:
            raise ValueError("No se puede eliminar la categoría por defecto.")
        cur = self.conn.cursor()
        cur.execute("UPDATE stores SET category_id = ? WHERE category_id = ?",
                    (default_id, category_id))
        cur.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        self.conn.commit()

    def get_categories(self) -> List[Tuple[int, str]]:
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM categories ORDER BY name ASC")
        return cur.fetchall()

    # Store operations
    def get_store_id(self, store_name: str) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM stores WHERE name = ?", (store_name,))
        row = cur.fetchone()
        if row:
            return row[0]
        default_cat = self.get_default_category_id()
        cur.execute("INSERT INTO stores (name, category_id) VALUES (?, ?)",
                    (store_name, default_cat))
        self.conn.commit()
        return cur.lastrowid

    def update_store_category(self, store_id: int, category_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE stores SET category_id = ? WHERE id = ?", (category_id, store_id))
        self.conn.commit()

    def get_store_category(self, store_id: int) -> Tuple[int, str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT c.id, c.name FROM stores s "
            "JOIN categories c ON s.category_id = c.id WHERE s.id = ?",
            (store_id,)
        )
        return cur.fetchone() or (self.get_default_category_id(), "NO ASIGNADA")

    def get_all_stores(self) -> List[Tuple[int, str, str]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT s.id, s.name, c.name FROM stores s "
            "LEFT JOIN categories c ON s.category_id = c.id ORDER BY s.name ASC"
        )
        return cur.fetchall()

    # Statement operations
    def add_statement(self, month: int, year: int, card_name: str, last4digits: str, file_path: str) -> int:
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO statements (month, year, card_name, last4digits, file_path)"
                " VALUES (?, ?, ?, ?, ?)",
                (month, year, card_name, last4digits, file_path)
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"Ya existe un resumen para {month:02d}/{year} de {card_name}.")

    def get_statements(self) -> List[Tuple[int, int, int, str, str]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, month, year, card_name, last4digits FROM statements "
            "ORDER BY year DESC, month DESC, card_name"
        )
        return cur.fetchall()

    def get_statement_file_path(self, statement_id: int) -> Optional[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT file_path FROM statements WHERE id = ?", (statement_id,))
        row = cur.fetchone()
        return row[0] if row else None

    # Transaction operations
    def add_transactions(self, statement_id: int, transactions: List[Transaction]) -> None:
        cur = self.conn.cursor()
        for t in transactions:
            store_id = self.get_store_id(t.store_name)
            cur.execute(
                "INSERT INTO transactions (statement_id, date, store_id, amount, installment_number)"
                " VALUES (?, ?, ?, ?, ?)",
                (statement_id, t.date.isoformat(), store_id, t.amount, t.installment_number)
            )
        self.conn.commit()

    def get_transactions_by_statement(self, statement_id: int) -> List[Tuple[int, str, str, float, Optional[int], int]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT t.id, t.date, s.name, t.amount, t.installment_number, s.id "
            "FROM transactions t JOIN stores s ON t.store_id = s.id "
            "WHERE t.statement_id = ? ORDER BY t.date ASC",
            (statement_id,)
        )
        return cur.fetchall()

    def get_category_sums(self, statement_id: int) -> List[Tuple[str, float]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT c.name, SUM(t.amount) AS total FROM transactions t "
            "JOIN stores s ON t.store_id = s.id "
            "JOIN categories c ON s.category_id = c.id "
            "WHERE t.statement_id = ? GROUP BY c.id ORDER BY total DESC",
            (statement_id,)
        )
        return cur.fetchall()

    def delete_statement(self, statement_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM statements WHERE id = ?", (statement_id,))
        self.conn.commit()


class PDFParser:
    """Parses credit card statement PDFs and extracts transactions."""

    DATE_PATTERN = re.compile(r"^(\d{1,2}[/-]\d{1,2})")
    DATE_MONTH_PATTERN = re.compile(r"^(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúÜü\.]+)")
    AMOUNT_PATTERN = re.compile(r"\d[\d\.]*,\d{2}-?")
    INSTALLMENT_C_PATTERN = re.compile(r"C\.?\s*(\d{1,2})/(\d{1,2})", re.IGNORECASE)
    MONTH_NAMES = { 'enero':1,'ene':1,'ene.':1,'febrero':2,'feb':2,'feb.':2,'marzo':3,'mar':3,'mar.':3,
                    'abril':4,'abr':4,'abr.':4,'mayo':5,'may':5,'may.':5,'junio':6,'jun':6,'jun.':6,
                    'julio':7,'jul':7,'jul.':7,'agosto':8,'ago':8,'ago.':8,'septiembre':9,'setiembre':9,
                    'sept':9,'sep':9,'sept.':9,'sep.':9,'octubre':10,'oct':10,'oct.':10,
                    'noviembre':11,'nov':11,'nov.':11,'diciembre':12,'dic':12,'dic.':12,'diciem.':12,'diciem':12 }

    def __init__(self) -> None:
        if not PYMUPDF_AVAILABLE:
            raise RuntimeError("PyMuPDF no está instalado. No se puede analizar PDFs.")

    def parse_pdf(self, pdf_path: str) -> List[Transaction]:
        doc = fitz.open(pdf_path)
        lines: List[str] = []
        for page in doc:
            text = page.get_text("text")
            lines.extend(text.split("\n"))
        doc.close()
        transactions: List[Transaction] = []
        for line in lines:
            original_line = line
            line = line.strip()
            if not line:
                continue
            date = None
            rest = None
            m = self.DATE_PATTERN.match(line)
            if m:
                date_str = m.group(1)
                day, month = map(int, date_str.replace('-', '/').split('/'))
                year = datetime.date.today().year
                try:
                    date = datetime.date(year, month, day)
                except ValueError:
                    date = None
                rest = line[m.end():].strip()
            if date is None:
                m2 = self.DATE_MONTH_PATTERN.match(line)
                if m2:
                    day = int(m2.group(1))
                    mn = m2.group(2).rstrip('.').lower()
                    month = self.MONTH_NAMES.get(mn)
                    if month:
                        year = datetime.date.today().year
                        date = datetime.date(year, month, day)
                        rest = line[m2.end():].strip()
            if date is None:
                hy = re.match(r"^(\d{1,2})-([A-Za-zÁÉÍÓÚáéíóúÜü]{3})-(\d{2,4})", line)
                if hy:
                    day = int(hy.group(1)); mon_abbr = hy.group(2).lower(); yearp = hy.group(3)
                    month = self.MONTH_NAMES.get(mon_abbr)
                    if month:
                        year = int(yearp) if len(yearp)==4 else 2000+int(yearp)
                        date = datetime.date(year, month, day)
                        rest = line[hy.end():].strip()
            if date is None or not rest:
                continue
            amt_match = None
            for mm in self.AMOUNT_PATTERN.finditer(rest):
                amt_match = mm
            if not amt_match:
                continue
            amt_str = amt_match.group(0)
            is_neg = '-' in amt_str or (amt_match.start()>0 and rest[amt_match.start()-1]=='-')
            amt_clean = amt_str.replace('.', '').replace(',', '.')
            try:
                amt = float(amt_clean)
                if is_neg:
                    amt = -amt
            except ValueError:
                continue
            desc = rest[:amt_match.start()].strip()
            inst = None
            im = self.INSTALLMENT_C_PATTERN.search(desc)
            if im:
                inst = int(im.group(1))
                desc = desc[:im.start()].strip()
            if '*' in desc:
                desc = desc.split('*',1)[1].strip()
            tokens = desc.split()
            cleaned = []
            skip=True
            for tok in tokens:
                if skip and re.fullmatch(r"\d+", tok): continue
                skip=False; cleaned.append(tok)
            desc = ' '.join(cleaned)
            if not desc:
                desc = original_line.strip()
            transactions.append(Transaction(date=date, store_name=desc, amount=amt, installment_number=inst))
        return transactions


class ExpenseApp:
    """Main Tkinter application for managing expenses."""

    def __init__(self, db_path: str = 'expenses.db') -> None:
        if not GUI_AVAILABLE:
            raise RuntimeError("Tkinter no está disponible.")
        if not MATPLOTLIB_AVAILABLE:
            raise RuntimeError("matplotlib no está disponible.")
        self.db = DatabaseManager(db_path)
        self.parser = PDFParser()
        self.root = tk.Tk()
        self.root.title("Gestor de Gastos de Tarjetas")
        self.root.geometry('1000x700')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.mainframe = ttk.Frame(self.root)
        self.mainframe.grid(row=0, column=0, sticky='nsew')
        self.mainframe.columnconfigure(0, weight=1)
        self.mainframe.rowconfigure(1, weight=1)
        self._build_top_controls()
        self._build_statement_list()
        self._build_transactions_view()
        self.refresh_statements()

    def _build_top_controls(self) -> None:
        top = ttk.Frame(self.mainframe)
        top.grid(row=0, column=0, sticky='ew', pady=5, padx=5)
        top.columnconfigure(5, weight=1)
        ttk.Button(top, text="Cargar Resumen", command=self.load_statement).grid(row=0, column=0, padx=5)
        ttk.Button(top, text="Gestionar Categorías", command=self.manage_categories).grid(row=0, column=1, padx=5)
        ttk.Button(top, text="Eliminar Resumen", command=self.delete_statement).grid(row=0, column=2, padx=5)
        ttk.Button(top, text="Exportar Excel", command=self.export_all_to_excel).grid(row=0, column=3, padx=5)
        ttk.Label(top, text="Seleccione un resumen para ver detalles").grid(row=0, column=4, padx=10, sticky='w')
        top.columnconfigure(4, weight=1)

    def _build_statement_list(self) -> None:
        f = ttk.Frame(self.mainframe)
        f.grid(row=1, column=0, sticky='nsew', padx=5, pady=5)
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        self.tree_statements = ttk.Treeview(f, columns=("mes","año","tarjeta"), show="headings")
        for col, txt in zip(("mes","año","tarjeta"), ("Mes","Año","Tarjeta")):
            self.tree_statements.heading(col, text=txt)
        self.tree_statements.bind("<<TreeviewSelect>>", self.on_statement_select)
        self.tree_statements.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(f, orient='vertical', command=self.tree_statements.yview)
        self.tree_statements.configure(yscroll=sb.set)
        sb.grid(row=0, column=1, sticky='ns')

    def _build_transactions_view(self) -> None:
        f = ttk.Frame(self.mainframe)
        f.grid(row=2, column=0, sticky='nsew', padx=5, pady=5)
        f.columnconfigure(0, weight=3)
        f.columnconfigure(1, weight=2)
        f.rowconfigure(0, weight=1)
        self.tree_transactions = ttk.Treeview(f, columns=("fecha","tienda","monto","cuota","categoria"), show="headings")
        for c, t in zip(("fecha","tienda","monto","cuota","categoria"), ("Fecha","Tienda","Monto","Cuota","Categoría")):
            self.tree_transactions.heading(c, text=t)
        self.tree_transactions.grid(row=0, column=0, sticky='nsew')
        sb2 = ttk.Scrollbar(f, orient='vertical', command=self.tree_transactions.yview)
        self.tree_transactions.configure(yscroll=sb2.set)
        sb2.grid(row=0, column=1, sticky='ns')
        self.figure = plt.Figure(figsize=(4,4))
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=f)
        self.canvas.get_tk_widget().grid(row=0, column=2, sticky='nsew', padx=10)
        self.tree_transactions.bind("<Double-1>", self.on_transaction_double_click)
        sf = ttk.Frame(f)
        sf.grid(row=1, column=0, columnspan=3, sticky='ew', pady=5)
        sf.columnconfigure(0, weight=3); sf.columnconfigure(1, weight=2)
        self.lbl_total = ttk.Label(sf, text="", font=(None,10,'bold'))
        self.lbl_total.grid(row=0, column=0, sticky='w')
        self.lbl_cat_totals = ttk.Label(sf, text="", justify='left')
        self.lbl_cat_totals.grid(row=0, column=1, sticky='w')

    def refresh_statements(self) -> None:
        for i in self.tree_statements.get_children():
            self.tree_statements.delete(i)
        for stmt in self.db.get_statements():
            sid, m, y, card, _ = stmt
            self.tree_statements.insert('', 'end', iid=str(sid), values=(f"{m:02d}", y, card))

    def on_statement_select(self, event) -> None:
        sel = self.tree_statements.selection()
        if not sel: return
        self.display_transactions(int(sel[0]))

    def display_transactions(self, statement_id: int) -> None:
        for i in self.tree_transactions.get_children():
            self.tree_transactions.delete(i)
        txns = self.db.get_transactions_by_statement(statement_id)
        for txn in txns:
            tid, date_iso, store, amt, inst, sid = txn
            cid, cname = self.db.get_store_category(sid)
            self.tree_transactions.insert('', 'end', iid=str(tid), values=(
                datetime.datetime.fromisoformat(date_iso).strftime("%d/%m/%Y"),
                store,
                f"{amt:,.2f}",
                inst or '',
                cname
            ))
        self.update_chart(statement_id)

    def update_chart(self, statement_id: int) -> None:
        self.ax.clear()
        sums = self.db.get_category_sums(statement_id)
        if not sums:
            self.ax.text(0.5,0.5,"Sin datos",ha='center',va='center')
            self.lbl_total.config(text=""); self.lbl_cat_totals.config(text="")
            self.canvas.draw(); return
        pos = [(n,t) for n,t in sums if t>0 and n.upper()!="NO APLICA"]
        if pos:
            labels, sizes = zip(*pos)
            self.ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90)
            self.ax.axis('equal')
            self.figure.suptitle('Gastos por Categoría')
        else:
            self.ax.text(0.5,0.5,"Sin datos",ha='center',va='center')
        self.canvas.draw()
        total = sum(t for _,t in sums)
        self.lbl_total.config(text=f"Total gastos del mes: $ {total:,.2f}")
        lines = [f"{n}: $ {t:,.2f}" for n,t in sums]
        self.lbl_cat_totals.config(text="\n".join(lines))

    def load_statement(self) -> None:
        file_path = filedialog.askopenfilename(filetypes=[('PDF files','*.pdf')])
        if not file_path: return
        fname = os.path.basename(file_path)
        default_month = datetime.date.today().month
        default_year = datetime.date.today().year
        m = re.search(r"(\d{4})[ -_]?(\d{2})", fname)
        if m:
            default_year, default_month = int(m.group(1)), int(m.group(2))
        ms = simpledialog.askstring("Mes","Ingrese el mes (1-12)",initialvalue=f"{default_month:02d}")
        if ms is None: return
        ys = simpledialog.askstring("Año","Ingrese el año",initialvalue=str(default_year))
        if ys is None: return
        cn = simpledialog.askstring("Nombre de Tarjeta","Ingrese el nombre de la tarjeta")
        if not cn: return
        try:
            month, year = int(ms), int(ys)
        except ValueError:
            messagebox.showerror("Error","Mes o año inválido."); return
        try:
            sid = self.db.add_statement(month, year, cn.strip(), "", file_path)
        except ValueError as e:
            messagebox.showerror("Error", str(e)); return
        try:
            txs = self.parser.parse_pdf(file_path)
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo analizar el PDF: {e}"); return
        adjusted = []
        for t in txs:
            try:
                nd = datetime.date(year, month, t.date.day)
                adjusted.append(Transaction(nd,t.store_name,t.amount,t.installment_number))
            except:
                pass
        if not adjusted:
            messagebox.showwarning("Sin datos","No se encontraron transacciones.")
        else:
            self.db.add_transactions(sid, adjusted)
            messagebox.showinfo("Éxito", f"Se cargaron {len(adjusted)} transacciones.")
        self.refresh_statements()

    def manage_categories(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Gestionar Categorías")
        dlg.geometry('300x400'); dlg.transient(self.root)
        tree = ttk.Treeview(dlg, columns=("nombre",), show="headings")
        tree.heading("nombre", text="Nombre de categoría")
        tree.grid(row=0, column=0, columnspan=2, sticky='nsew', padx=5, pady=5)
        sb = ttk.Scrollbar(dlg, orient='vertical', command=tree.yview)
        tree.configure(yscroll=sb.set); sb.grid(row=0, column=2, sticky='ns')
        dlg.rowconfigure(0, weight=1); dlg.columnconfigure(1, weight=1)
        def refresh_list():
            for i in tree.get_children(): tree.delete(i)
            for cid,n in self.db.get_categories(): tree.insert('', 'end', iid=str(cid), values=(n,))
        def add_cat():
            nm = simpledialog.askstring("Nueva Categoría","Ingrese nombre")
            if nm:
                try: self.db.add_category(nm.strip()); refresh_list()
                except ValueError as e: messagebox.showerror("Error",str(e))
        def del_cat():
            sel = tree.selection()
            if sel and int(sel[0])!=self.db.get_default_category_id():
                if messagebox.askyesno("Confirmar","Eliminar categoría?"):
                    self.db.delete_category(int(sel[0])); refresh_list()
                    sel2 = self.tree_statements.selection()
                    if sel2: self.display_transactions(int(sel2[0]))
        ttk.Button(dlg,text="Agregar",command=add_cat).grid(row=1,column=0,padx=5,pady=5,sticky='ew')
        ttk.Button(dlg,text="Eliminar",command=del_cat).grid(row=1,column=1,padx=5,pady=5,sticky='ew')
        refresh_list(); dlg.grab_set(); dlg.wait_window()

    def delete_statement(self) -> None:
        sel = self.tree_statements.selection()
        if not sel:
            messagebox.showinfo("Eliminar resumen","Seleccione un resumen.")
            return
        sid=int(sel[0])
        if not messagebox.askyesno("Confirmar eliminación","Borrar resumen y transacciones?"):
            return
        self.db.delete_statement(sid)
        for i in self.tree_transactions.get_children(): self.tree_transactions.delete(i)
        self.ax.clear(); self.ax.text(0.5,0.5,"Sin datos",ha='center',va='center'); self.canvas.draw()
        self.lbl_total.config(text=''); self.lbl_cat_totals.config(text='')
        self.refresh_statements()

    def on_transaction_double_click(self, event) -> None:
        row=self.tree_transactions.identify_row(event.y)
        if not row: return
        tid=int(row)
        cur=self.db.conn.cursor()
        cur.execute("SELECT store_id FROM transactions WHERE id=?",(tid,))
        r=cur.fetchone()
        if not r: return
        store_id=r[0]
        cats=self.db.get_categories()
        names=[n for _,n in cats]; ids=[i for i,_ in cats]
        curr_id,_=self.db.get_store_category(store_id)
        dlg=tk.Toplevel(self.root); dlg.title("Asignar Categoría"); dlg.transient(self.root)
        ttk.Label(dlg,text="Seleccione una categoría:").grid(row=0,column=0,padx=10,pady=10)
        combo=ttk.Combobox(dlg,values=names,state='readonly'); combo.grid(row=1,column=0,padx=10,pady=10)
        try: combo.current(ids.index(curr_id))
        except: pass
        def ok():
            idx=combo.current()
            if idx>=0: self.db.update_store_category(store_id, ids[idx]);
            sel2=self.tree_statements.selection()
            if sel2: self.display_transactions(int(sel2[0]))
            dlg.destroy()
        ttk.Button(dlg,text="Aceptar",command=ok).grid(row=2,column=0,padx=10,pady=10)
        dlg.grab_set(); dlg.wait_window()

    def export_all_to_excel(self) -> None:
        """Exporta todas las transacciones de todas las tarjetas y periodos a un archivo .xlsx."""
        query = (
            "SELECT stm.year AS Año, stm.month AS Mes, stm.card_name AS Tarjeta, "
            "t.date AS Fecha, s.name AS Comercio, t.amount AS Monto, c.name AS Categoría "
            "FROM transactions t "
            "JOIN statements stm ON t.statement_id = stm.id "
            "JOIN stores s ON t.store_id = s.id "
            "JOIN categories c ON s.category_id = c.id "
            "ORDER BY stm.year DESC, stm.month DESC, stm.card_name, t.date"
        )
        df=pd.read_sql_query(query, self.db.conn)
        path=filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files","*.xlsx"),("All","*.*")], title="Guardar reporte de gastos")
        if not path: return
        try:
            df.to_excel(path, index=False, sheet_name="Gastos")
            messagebox.showinfo("Exportar Excel", f"Reporte guardado en:\n{path}")
        except Exception as e:
            messagebox.showerror("Error al exportar", str(e))

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not GUI_AVAILABLE:
        print("La interfaz gráfica no está disponible.")
        return
    try:
        ExpenseApp().run()
    except RuntimeError as e:
        print(str(e))

if __name__ == '__main__':
    main()






# #!/usr/bin/env python3
# """
# Expense Manager Application
# ==========================
#
# This module implements a desktop application for tracking and categorising
# credit‑card expenses. It reads PDF statements from Visa and Mastercard,
# extracts individual transactions, persists them in a SQLite database,
# allows users to map merchant names to categories, and presents an
# interactive view of spending, including summary charts.
#
# Key Features
# ------------
# * **Persistent database**: All data is stored in a SQLite database, allowing
#   the application state to survive between runs.
# * **PDF import**: Statements from different card providers are parsed
#   automatically using heuristics to identify the date, merchant, amount and
#   instalment number from each line in the PDF. Unsupported or malformed
#   lines are skipped gracefully.
# * **Category management**: Users can add or delete categories at any time.
#   A default category (“NO ASIGNADA”) ensures that uncategorised expenses are
#   clearly separated.
# * **Merchant mapping**: The relationship between merchant names and
#   categories is persisted. Updating a mapping immediately affects all
#   existing transactions associated with that merchant.
# * **Interactive UI**: The application presents statements and their
#   transactions in a table, allows inline editing of merchant categories
#   via dropdowns, and displays a pie chart of spending by category for the
#   selected month.
#
# Note
# ----
# This application uses the Tkinter GUI toolkit. If you run this code in
# an environment without a display (e.g. a headless server), the GUI will
# not launch. Nevertheless, the core database and PDF parsing logic can still
# be exercised programmatically.
#
# """
#
# import os
# import re
# import sqlite3
# import datetime
# from dataclasses import dataclass, field
# from typing import List, Optional, Tuple
#
# # Attempt to import GUI and plotting libraries. These modules may be
# # unavailable in headless environments. We delay their import inside the
# # classes that need them so that the non‑GUI parts of the code can still be
# # loaded and tested without requiring a display.
# try:
#     import tkinter as tk
#     from tkinter import ttk, filedialog, messagebox, simpledialog
#     GUI_AVAILABLE = True
# except Exception:
#     GUI_AVAILABLE = False
#
# try:
#     import matplotlib
#     # Use a non-interactive backend for embedding in Tkinter
#     matplotlib.use('Agg')
#     import matplotlib.pyplot as plt
#     from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
#     MATPLOTLIB_AVAILABLE = True
# except Exception:
#     MATPLOTLIB_AVAILABLE = False
#
# try:
#     # PyMuPDF library for extracting text from PDFs
#     import fitz  # type: ignore
#     PYMUPDF_AVAILABLE = True
# except Exception:
#     PYMUPDF_AVAILABLE = False
#
#
# @dataclass
# class Transaction:
#     """Represents a single transaction extracted from a statement."""
#     date: datetime.date
#     store_name: str
#     amount: float
#     installment_number: Optional[int] = None
#
#
# class DatabaseManager:
#     """Encapsulates all database interactions."""
#
#     def __init__(self, db_path: str = 'expenses.db') -> None:
#         self.db_path = db_path
#         self.conn = sqlite3.connect(self.db_path)
#         # Enable foreign keys
#         self.conn.execute('PRAGMA foreign_keys = ON;')
#         self._create_tables()
#         self._ensure_default_category()
#
#     def _create_tables(self) -> None:
#         """Create tables if they do not already exist."""
#         cur = self.conn.cursor()
#         cur.execute(
#             """
#             CREATE TABLE IF NOT EXISTS categories (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 name TEXT UNIQUE NOT NULL
#             );
#             """
#         )
#         cur.execute(
#             """
#             CREATE TABLE IF NOT EXISTS stores (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 name TEXT UNIQUE NOT NULL,
#                 category_id INTEGER,
#                 FOREIGN KEY (category_id) REFERENCES categories(id)
#             );
#             """
#         )
#         cur.execute(
#             """
#             CREATE TABLE IF NOT EXISTS statements (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 month INTEGER NOT NULL,
#                 year INTEGER NOT NULL,
#                 card_name TEXT NOT NULL,
#                 last4digits TEXT NOT NULL,
#                 file_path TEXT,
#                 UNIQUE (month, year, card_name, last4digits)
#             );
#             """
#         )
#         cur.execute(
#             """
#             CREATE TABLE IF NOT EXISTS transactions (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 statement_id INTEGER NOT NULL,
#                 date TEXT NOT NULL,
#                 store_id INTEGER NOT NULL,
#                 amount REAL NOT NULL,
#                 installment_number INTEGER,
#                 FOREIGN KEY (statement_id) REFERENCES statements(id) ON DELETE CASCADE,
#                 FOREIGN KEY (store_id) REFERENCES stores(id)
#             );
#             """
#         )
#         self.conn.commit()
#
#     def _ensure_default_category(self) -> None:
#         """Ensure there is a default 'NO ASIGNADA' category."""
#         cur = self.conn.cursor()
#         cur.execute("SELECT id FROM categories WHERE name = ?", ("NO ASIGNADA",))
#         if cur.fetchone() is None:
#             cur.execute("INSERT INTO categories (name) VALUES (?)", ("NO ASIGNADA",))
#             self.conn.commit()
#
#     # Category operations
#     def add_category(self, name: str) -> None:
#         cur = self.conn.cursor()
#         try:
#             cur.execute("INSERT INTO categories (name) VALUES (?)", (name,))
#             self.conn.commit()
#         except sqlite3.IntegrityError:
#             raise ValueError(f"La categoría '{name}' ya existe.")
#
#     def delete_category(self, category_id: int) -> None:
#         """Delete a category. Reassign any stores mapped to this category to the default."""
#         default_id = self.get_default_category_id()
#         if category_id == default_id:
#             raise ValueError("No se puede eliminar la categoría por defecto.")
#         cur = self.conn.cursor()
#         # Reassign stores to default category
#         cur.execute(
#             "UPDATE stores SET category_id = ? WHERE category_id = ?",
#             (default_id, category_id),
#         )
#         # Delete the category
#         cur.execute("DELETE FROM categories WHERE id = ?", (category_id,))
#         self.conn.commit()
#
#     def get_default_category_id(self) -> int:
#         cur = self.conn.cursor()
#         cur.execute("SELECT id FROM categories WHERE name = ?", ("NO ASIGNADA",))
#         row = cur.fetchone()
#         if row is None:
#             # Should never happen because ensure_default_category has run
#             return 1
#         return row[0]
#
#     def get_categories(self) -> List[Tuple[int, str]]:
#         cur = self.conn.cursor()
#         cur.execute("SELECT id, name FROM categories ORDER BY name ASC")
#         return cur.fetchall()
#
#     # Store operations
#     def get_store_id(self, store_name: str) -> int:
#         """Return the ID for a store, inserting it if necessary with default category."""
#         cur = self.conn.cursor()
#         cur.execute("SELECT id, category_id FROM stores WHERE name = ?", (store_name,))
#         row = cur.fetchone()
#         if row:
#             return row[0]
#         # Insert new store with default category
#         default_category = self.get_default_category_id()
#         cur.execute(
#             "INSERT INTO stores (name, category_id) VALUES (?, ?)",
#             (store_name, default_category),
#         )
#         self.conn.commit()
#         return cur.lastrowid
#
#     def update_store_category(self, store_id: int, category_id: int) -> None:
#         cur = self.conn.cursor()
#         cur.execute(
#             "UPDATE stores SET category_id = ? WHERE id = ?",
#             (category_id, store_id),
#         )
#         self.conn.commit()
#
#     def get_store_category(self, store_id: int) -> Tuple[int, str]:
#         cur = self.conn.cursor()
#         cur.execute(
#             "SELECT c.id, c.name FROM stores s JOIN categories c ON s.category_id = c.id WHERE s.id = ?",
#             (store_id,),
#         )
#         return cur.fetchone()
#
#     def get_all_stores(self) -> List[Tuple[int, str, str]]:
#         """Return a list of all stores with their assigned category names."""
#         cur = self.conn.cursor()
#         cur.execute(
#             """
#             SELECT s.id, s.name, c.name
#             FROM stores s
#             LEFT JOIN categories c ON s.category_id = c.id
#             ORDER BY s.name ASC
#             """
#         )
#         return cur.fetchall()
#
#     # Statement operations
#     def add_statement(self, month: int, year: int, card_name: str, last4digits: str, file_path: str) -> int:
#         cur = self.conn.cursor()
#         try:
#             cur.execute(
#                 """
#                 INSERT INTO statements (month, year, card_name, last4digits, file_path)
#                 VALUES (?, ?, ?, ?, ?)
#                 """,
#                 (month, year, card_name, last4digits, file_path),
#             )
#             self.conn.commit()
#             return cur.lastrowid
#         except sqlite3.IntegrityError:
#             raise ValueError(
#                 f"Ya existe un resumen para {month:02d}/{year} de {card_name}."
#             )
#
#     def get_statements(self) -> List[Tuple[int, int, int, str, str]]:
#         cur = self.conn.cursor()
#         cur.execute(
#             "SELECT id, month, year, card_name, last4digits FROM statements ORDER BY year DESC, month DESC, card_name"
#         )
#         return cur.fetchall()
#
#     def get_statement_file_path(self, statement_id: int) -> Optional[str]:
#         cur = self.conn.cursor()
#         cur.execute("SELECT file_path FROM statements WHERE id = ?", (statement_id,))
#         row = cur.fetchone()
#         return row[0] if row else None
#
#     # Transaction operations
#     def add_transactions(self, statement_id: int, transactions: List[Transaction]) -> None:
#         cur = self.conn.cursor()
#         for t in transactions:
#             store_id = self.get_store_id(t.store_name)
#             cur.execute(
#                 """
#                 INSERT INTO transactions (statement_id, date, store_id, amount, installment_number)
#                 VALUES (?, ?, ?, ?, ?)
#                 """,
#                 (
#                     statement_id,
#                     t.date.isoformat(),
#                     store_id,
#                     t.amount,
#                     t.installment_number,
#                 ),
#             )
#         self.conn.commit()
#
#     def get_transactions_by_statement(self, statement_id: int) -> List[Tuple[int, str, str, float, Optional[int], int]]:
#         """
#         Return transactions for a statement as a list of tuples:
#         (transaction_id, date_iso, store_name, amount, installment_number, store_id)
#         """
#         cur = self.conn.cursor()
#         cur.execute(
#             """
#             SELECT t.id, t.date, s.name, t.amount, t.installment_number, s.id
#             FROM transactions t
#             JOIN stores s ON t.store_id = s.id
#             WHERE t.statement_id = ?
#             ORDER BY t.date ASC
#             """,
#             (statement_id,),
#         )
#         return cur.fetchall()
#
#     def get_category_sums(self, statement_id: int) -> List[Tuple[str, float]]:
#         """Return list of (category_name, total_amount) for a statement."""
#         cur = self.conn.cursor()
#         cur.execute(
#             """
#             SELECT c.name, SUM(t.amount) AS total
#             FROM transactions t
#             JOIN stores s ON t.store_id = s.id
#             JOIN categories c ON s.category_id = c.id
#             WHERE t.statement_id = ?
#             GROUP BY c.id
#             ORDER BY total DESC
#             """,
#             (statement_id,),
#         )
#         return cur.fetchall()
#
#     def delete_statement(self, statement_id: int) -> None:
#         """Delete a statement and its associated transactions."""
#         cur = self.conn.cursor()
#         cur.execute("DELETE FROM statements WHERE id = ?", (statement_id,))
#         self.conn.commit()
#
#
# class PDFParser:
#     """Parses credit card statement PDFs and extracts transactions."""
#
#     # Pattern for dates in numeric form (e.g. 12/05 or 3-11)
#     DATE_PATTERN = re.compile(r"^(\d{1,2}[/-]\d{1,2})")
#     # Pattern for dates with month names (e.g. "25 Junio", "4 Dic.", "12 Oct")
#     DATE_MONTH_PATTERN = re.compile(r"^(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúÜü\.]+)")
#     # Pattern to find amounts with optional thousand separators ('.' as separator) and a comma for decimals.
#     # It matches numbers like '9.583,29', '2647.143,35', '83,50' and allows an optional trailing minus sign.
#     AMOUNT_PATTERN = re.compile(r"\d[\d\.]*,\d{2}-?")
#     # Pattern to find instalment information (e.g. C.09/12)
#     INSTALLMENT_C_PATTERN = re.compile(r"C\.?\s*(\d{1,2})/(\d{1,2})", re.IGNORECASE)
#
#     # Map various Spanish month names and abbreviations to month numbers
#     MONTH_NAMES = {
#         'enero': 1, 'ene': 1, 'ene.': 1,
#         'febrero': 2, 'feb': 2, 'feb.': 2,
#         'marzo': 3, 'mar': 3, 'mar.': 3,
#         'abril': 4, 'abr': 4, 'abr.': 4,
#         'mayo': 5, 'may': 5, 'may.': 5,
#         'junio': 6, 'jun': 6, 'jun.': 6,
#         'julio': 7, 'jul': 7, 'jul.': 7,
#         'agosto': 8, 'ago': 8, 'ago.': 8,
#         'septiembre': 9, 'setiembre': 9, 'sept': 9, 'sep': 9, 'sept.': 9, 'sep.': 9,
#         'octubre': 10, 'oct': 10, 'oct.': 10,
#         'noviembre': 11, 'nov': 11, 'nov.': 11,
#         'diciembre': 12, 'dic': 12, 'dic.': 12, 'diciem.': 12, 'diciem': 12,
#     }
#
#     def __init__(self) -> None:
#         if not PYMUPDF_AVAILABLE:
#             raise RuntimeError(
#                 "PyMuPDF no está instalado. No se puede analizar archivos PDF."
#             )
#
#     def parse_pdf(self, pdf_path: str) -> List[Transaction]:
#         """
#         Extract a list of transactions from a credit card statement PDF. The
#         parser attempts to identify lines that contain a date at the start,
#         followed by a merchant name and an amount. Some statements include
#         instalment information, which we attempt to capture as well.
#
#         :param pdf_path: Path to the PDF file
#         :return: List of Transaction objects
#         """
#         doc = fitz.open(pdf_path)
#         lines: List[str] = []
#         # Extract text from all pages
#         for page in doc:
#             text = page.get_text("text")
#             page_lines = text.split("\n")
#             lines.extend(page_lines)
#         doc.close()
#         transactions: List[Transaction] = []
#         for line in lines:
#             original_line = line
#             # Strip whitespace and ignore empty lines
#             line = line.strip()
#             if not line:
#                 continue
#             # Identify lines starting with a date pattern (numeric)
#             date = None
#             rest = None
#             date_match_numeric = self.DATE_PATTERN.match(line)
#             if date_match_numeric:
#                 date_str = date_match_numeric.group(1)
#                 try:
#                     day_month = date_str.replace("-", "/").split("/")
#                     day = int(day_month[0])
#                     month = int(day_month[1])
#                     current_year = datetime.date.today().year
#                     date = datetime.date(current_year, month, day)
#                     rest = line[len(date_str):].strip()
#                 except Exception:
#                     pass
#             else:
#                 # Try month names pattern
#                 match_month = self.DATE_MONTH_PATTERN.match(line)
#                 if match_month:
#                     day_str, month_name = match_month.groups()
#                     day_str = day_str.strip('.').strip()
#                     day = None
#                     try:
#                         day = int(day_str)
#                     except Exception:
#                         pass
#                     mn = month_name.strip('.').lower()
#                     month = self.MONTH_NAMES.get(mn)
#                     if day is not None and month is not None:
#                         # Use current year; actual year will be provided by metadata
#                         current_year = datetime.date.today().year
#                         try:
#                             date = datetime.date(current_year, month, day)
#                             rest = line[len(match_month.group(0)):].strip()
#                         except Exception:
#                             pass
#             # If still no date, try pattern like '24-May-25' with hyphen
#             if date is None:
#                 hyphen_match = re.match(r"^(\d{1,2})-([A-Za-zÁÉÍÓÚáéíóúÜü]{3})-(\d{2,4})", line)
#                 if hyphen_match:
#                     day = int(hyphen_match.group(1))
#                     month_abbr = hyphen_match.group(2).lower()
#                     year_part = hyphen_match.group(3)
#                     # Convert month abbreviation to full names if necessary (e.g., 'may' -> 5)
#                     month = self.MONTH_NAMES.get(month_abbr)
#                     if month:
#                         # Convert year: if 2 digits, assume 2000s
#                         if len(year_part) == 2:
#                             year = 2000 + int(year_part)
#                         else:
#                             year = int(year_part)
#                         try:
#                             date = datetime.date(year, month, day)
#                             rest = line[len(hyphen_match.group(0)):].strip()
#                         except Exception:
#                             date = None
#             if date is None:
#                 continue
#             if rest is None:
#                 rest = ''
#             # Find the amount in the line (last occurrence)
#             amount_match = None
#             for m in self.AMOUNT_PATTERN.finditer(rest):
#                 amount_match = m
#             if not amount_match:
#                 continue
#             amount_str = amount_match.group(0)
#             # Determine if amount is negative. A minus sign can appear at the
#             # beginning of the number (e.g. -9990,00) or at the end after the
#             # decimals (e.g. 6.800,00-). We check both cases.
#             is_negative = False
#             # If the matched string itself contains a '-' sign, treat as negative.
#             if '-' in amount_str:
#                 is_negative = True
#                 amount_str = amount_str.replace('-', '')
#             # If there's a '-' character immediately before the matched number in the rest string, treat as negative
#             else:
#                 idx_start = amount_match.start()
#                 if idx_start > 0 and rest[idx_start - 1] == '-':
#                     is_negative = True
#             # Clean thousand separators and convert decimal comma to dot
#             amount_clean = amount_str.replace('.', '').replace(',', '.')
#             try:
#                 amount = float(amount_clean)
#                 if is_negative:
#                     amount = -amount
#             except ValueError:
#                 continue
#             # Extract the substring before the amount as the merchant and other info
#             desc_segment = rest[:amount_match.start()].rstrip()
#             # Identify and extract instalment information (C.xx/yy)
#             installment_number: Optional[int] = None
#             inst_match = self.INSTALLMENT_C_PATTERN.search(desc_segment)
#             if inst_match:
#                 try:
#                     installment_number = int(inst_match.group(1))
#                 except Exception:
#                     pass
#                 # Remove instalment text from description segment
#                 desc_segment = desc_segment[:inst_match.start()].rstrip()
#             # Further cleaning of description segment
#             description = desc_segment
#             # If there's an asterisk, assume description starts after it
#             if '*' in description:
#                 # If an asterisk is present, assume the description begins after it
#                 description = description.split('*', 1)[1].strip()
#                 # Remove trailing tokens that are purely numeric or a lone hyphen (e.g. coupon numbers or negative markers)
#                 tokens = description.split()
#                 while tokens and (re.fullmatch(r"\d+", tokens[-1]) or tokens[-1] == '-'):
#                     tokens.pop()
#                 description = ' '.join(tokens).strip()
#             else:
#                 # Otherwise, attempt to skip leading numeric tokens
#                 tokens = description.split()
#                 keep: List[str] = []
#                 skip = True
#                 for token in tokens:
#                     if skip:
#                         # Skip tokens that are purely numeric (digits)
#                         if re.fullmatch(r"\d+", token):
#                             continue
#                         # Some tokens like 'K' may appear before description; skip them
#                         if token.upper() in {'K'}:
#                             continue
#                         # Once we hit a token containing a letter, start keeping
#                         skip = False
#                     keep.append(token)
#                 # Remove trailing tokens that are purely numeric or a lone hyphen (e.g. coupon numbers)
#                 while keep and (re.fullmatch(r"\d+", keep[-1]) or keep[-1] == '-'):
#                     keep.pop()
#                 description = ' '.join(keep).strip()
#             # At this point, description may still contain instalment info like '11/12' for Mastercard.
#             # Extract instalment number if none found yet.
#             if installment_number is None:
#                 tokens = description.split()
#                 to_remove = None
#                 for token in tokens:
#                     # Match patterns like '05/06' or '5/12'
#                     if re.fullmatch(r"\d{1,2}/\d{1,2}", token):
#                         try:
#                             installment_number = int(token.split('/')[0])
#                             to_remove = token
#                             break
#                         except Exception:
#                             pass
#                 # Remove the token from description
#                 if to_remove:
#                     tokens = [t for t in tokens if t != to_remove]
#                     description = ' '.join(tokens)
#             # Collapse multiple spaces
#             description = re.sub(r"\s{2,}", " ", description).strip()
#             if not description:
#                 # If we failed to extract a description, attempt to keep the original trimmed line
#                 description = original_line.strip()
#             transactions.append(
#                 Transaction(
#                     date=date,
#                     store_name=description,
#                     amount=amount,
#                     installment_number=installment_number,
#                 )
#             )
#         return transactions
#
#
# class ExpenseApp:
#     """Main Tkinter application for managing expenses."""
#
#     def __init__(self, db_path: str = 'expenses.db') -> None:
#         if not GUI_AVAILABLE:
#             raise RuntimeError(
#                 "No se puede iniciar la interfaz gráfica porque Tkinter no está disponible."
#             )
#         if not MATPLOTLIB_AVAILABLE:
#             raise RuntimeError(
#                 "No se puede crear gráficos porque matplotlib no está disponible."
#             )
#         # Create database manager
#         self.db = DatabaseManager(db_path)
#         # PDF parser
#         self.parser = PDFParser()
#         # Root window
#         self.root = tk.Tk()
#         self.root.title("Gestor de Gastos de Tarjetas")
#         self.root.geometry('1000x700')
#         # Configure rows and columns
#         self.root.columnconfigure(0, weight=1)
#         self.root.rowconfigure(0, weight=1)
#         # Create main frames
#         self.mainframe = ttk.Frame(self.root)
#         self.mainframe.grid(row=0, column=0, sticky='nsew')
#         self.mainframe.columnconfigure(0, weight=1)
#         self.mainframe.rowconfigure(1, weight=1)
#         # Controls at top
#         self._build_top_controls()
#         # Statement list and details area
#         self._build_statement_list()
#         # Transaction table and summary chart
#         self._build_transactions_view()
#         # Populate statement list
#         self.refresh_statements()
#
#     def _build_top_controls(self) -> None:
#         top_frame = ttk.Frame(self.mainframe)
#         top_frame.grid(row=0, column=0, sticky='ew', pady=5, padx=5)
#         top_frame.columnconfigure(5, weight=1)
#         # Button to load new statement
#         self.btn_load = ttk.Button(top_frame, text="Cargar Resumen", command=self.load_statement)
#         self.btn_load.grid(row=0, column=0, padx=5)
#         # Button to manage categories
#         self.btn_manage_categories = ttk.Button(top_frame, text="Gestionar Categorías", command=self.manage_categories)
#         self.btn_manage_categories.grid(row=0, column=1, padx=5)
#         # Button to delete selected statement
#         self.btn_delete_statement = ttk.Button(top_frame, text="Eliminar Resumen", command=self.delete_statement)
#         self.btn_delete_statement.grid(row=0, column=2, padx=5)
#         # Instructions label
#         lbl = ttk.Label(top_frame, text="Seleccione un resumen para ver los detalles")
#         # Position the label in a separate column to avoid overlapping buttons
#         lbl.grid(row=0, column=3, padx=10, sticky='w')
#
#     def _build_statement_list(self) -> None:
#         frame = ttk.Frame(self.mainframe)
#         frame.grid(row=1, column=0, sticky='nsew', padx=5, pady=5)
#         frame.rowconfigure(0, weight=1)
#         frame.columnconfigure(0, weight=1)
#         # Treeview for statements
#         # Define columns for month, year and card name only (we no longer track last 4 digits)
#         self.tree_statements = ttk.Treeview(frame, columns=("mes", "año", "tarjeta"), show="headings")
#         self.tree_statements.heading("mes", text="Mes")
#         self.tree_statements.heading("año", text="Año")
#         self.tree_statements.heading("tarjeta", text="Tarjeta")
#         self.tree_statements.bind("<<TreeviewSelect>>", self.on_statement_select)
#         self.tree_statements.grid(row=0, column=0, sticky='nsew')
#         # Scrollbar
#         scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.tree_statements.yview)
#         self.tree_statements.configure(yscroll=scrollbar.set)
#         scrollbar.grid(row=0, column=1, sticky='ns')
#
#     def _build_transactions_view(self) -> None:
#         frame = ttk.Frame(self.mainframe)
#         frame.grid(row=2, column=0, sticky='nsew', padx=5, pady=5)
#         frame.columnconfigure(0, weight=3)
#         frame.columnconfigure(1, weight=2)
#         frame.rowconfigure(0, weight=1)
#         # Transaction table
#         self.tree_transactions = ttk.Treeview(frame, columns=("fecha", "tienda", "monto", "cuota", "categoria"), show="headings")
#         for col, text in zip(("fecha", "tienda", "monto", "cuota", "categoria"), ["Fecha", "Tienda", "Monto", "Cuota", "Categoría"]):
#             self.tree_transactions.heading(col, text=text)
#         self.tree_transactions.grid(row=0, column=0, sticky='nsew')
#         # Scrollbar
#         scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.tree_transactions.yview)
#         self.tree_transactions.configure(yscroll=scrollbar.set)
#         scrollbar.grid(row=0, column=1, sticky='ns')
#         # Pie chart area
#         self.figure = plt.Figure(figsize=(4, 4))
#         self.ax = self.figure.add_subplot(111)
#         self.canvas = FigureCanvasTkAgg(self.figure, master=frame)
#         self.canvas.get_tk_widget().grid(row=0, column=2, sticky='nsew', padx=10)
#         # Bind double click on transaction row to edit store category
#         self.tree_transactions.bind("<Double-1>", self.on_transaction_double_click)
#         # Summary labels (total and per-category totals)
#         summary_frame = ttk.Frame(frame)
#         summary_frame.grid(row=1, column=0, columnspan=3, sticky='ew', pady=5)
#         summary_frame.columnconfigure(0, weight=3)
#         summary_frame.columnconfigure(1, weight=2)
#         # Total spending label
#         self.lbl_total = ttk.Label(summary_frame, text="", font=("TkDefaultFont", 10, "bold"))
#         self.lbl_total.grid(row=0, column=0, sticky='w')
#         # Category totals label
#         self.lbl_cat_totals = ttk.Label(summary_frame, text="", justify='left')
#         self.lbl_cat_totals.grid(row=0, column=1, sticky='w')
#
#     # UI event handlers
#     def refresh_statements(self) -> None:
#         """Reload the statements list from the database."""
#         for item in self.tree_statements.get_children():
#             self.tree_statements.delete(item)
#         for stmt in self.db.get_statements():
#             stmt_id, month, year, card_name, _last4 = stmt
#             self.tree_statements.insert('', 'end', iid=str(stmt_id), values=(f"{month:02d}", year, card_name))
#
#     def on_statement_select(self, event) -> None:
#         selected = self.tree_statements.selection()
#         if not selected:
#             return
#         stmt_id = int(selected[0])
#         self.display_transactions(stmt_id)
#
#     def display_transactions(self, statement_id: int) -> None:
#         """Display the transactions and category breakdown for the selected statement."""
#         # Clear existing rows
#         for item in self.tree_transactions.get_children():
#             self.tree_transactions.delete(item)
#         # Load transactions
#         txns = self.db.get_transactions_by_statement(statement_id)
#         for txn in txns:
#             txn_id, date_iso, store_name, amount, installment, store_id = txn
#             category_id, category_name = self.db.get_store_category(store_id)
#             self.tree_transactions.insert(
#                 '', 'end', iid=str(txn_id), values=(
#                     datetime.datetime.fromisoformat(date_iso).strftime("%d/%m/%Y"),
#                     store_name,
#                     f"{amount:,.2f}",
#                     installment if installment is not None else '',
#                     category_name,
#                 )
#             )
#         # Update pie chart
#         self.update_chart(statement_id)
#
#     def update_chart(self, statement_id: int) -> None:
#         """Redraw the pie chart for the selected statement."""
#         self.ax.clear()
#         category_sums = self.db.get_category_sums(statement_id)
#         if not category_sums:
#             # If there are no transactions, clear chart and summaries
#             self.ax.text(0.5, 0.5, "Sin datos", ha='center', va='center')
#             # Clear summary labels
#             if hasattr(self, 'lbl_total'):
#                 self.lbl_total.config(text="")
#             if hasattr(self, 'lbl_cat_totals'):
#                 self.lbl_cat_totals.config(text="")
#             self.canvas.draw()
#             return
#         # Separate positive totals for pie chart (negative values cannot be drawn in a pie chart)
#         positive_categories = [(name, total) for name, total in category_sums if total > 0]
#         if positive_categories:
#             labels = [name for name, total in positive_categories]
#             sizes = [total for name, total in positive_categories]
#             self.ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90)
#             self.ax.axis('equal')
#             self.figure.suptitle('Gastos por Categoría')
#         else:
#             # If no positive totals, indicate no spending data
#             self.ax.text(0.5, 0.5, "Sin datos", ha='center', va='center')
#         self.canvas.draw()
#         # Compute net total spending (sum of all category totals, including negatives)
#         total_spent = sum(total for _, total in category_sums)
#         # Update summary labels
#         if hasattr(self, 'lbl_total'):
#             self.lbl_total.config(text=f"Total gastos del mes: $ {total_spent:,.2f}")
#         if hasattr(self, 'lbl_cat_totals'):
#             lines = []
#             for name, total in category_sums:
#                 lines.append(f"{name}: $ {total:,.2f}")
#             self.lbl_cat_totals.config(text="\n".join(lines))
#
#     def load_statement(self) -> None:
#         """Handler to load a PDF statement and insert it into the database."""
#         # Ask user to select PDF file
#         file_path = filedialog.askopenfilename(filetypes=[('PDF files', '*.pdf')])
#         if not file_path:
#             return
#         # Ask for statement metadata (month, year, card name, last 4 digits)
#         # Attempt to infer from file name
#         filename = os.path.basename(file_path)
#         default_month = datetime.date.today().month
#         default_year = datetime.date.today().year
#         # Simple inference: find YYYY or YY and MM in filename
#         match = re.search(r"(\d{4})[ -_]?(\d{2})", filename)
#         if match:
#             default_year = int(match.group(1))
#             default_month = int(match.group(2))
#         # Ask user
#         month_str = simpledialog.askstring("Mes", "Ingrese el mes (1-12)", initialvalue=f"{default_month:02d}")
#         if month_str is None:
#             return
#         year_str = simpledialog.askstring("Año", "Ingrese el año", initialvalue=str(default_year))
#         if year_str is None:
#             return
#         card_name = simpledialog.askstring("Nombre de Tarjeta", "Ingrese el nombre de la tarjeta (e.g. Visa, Mastercard)")
#         if card_name is None or card_name.strip() == "":
#             return
#         # Do not ask for last 4 digits; use empty string. This simplifies uniqueness to (mes, año, tarjeta).
#         last4 = ""
#         try:
#             month = int(month_str)
#             year = int(year_str)
#         except ValueError:
#             messagebox.showerror("Error", "Mes o año inválido.")
#             return
#         try:
#             statement_id = self.db.add_statement(month, year, card_name.strip(), last4, file_path)
#         except ValueError as e:
#             messagebox.showerror("Error", str(e))
#             return
#         # Parse PDF
#         try:
#             transactions = self.parser.parse_pdf(file_path)
#         except Exception as e:
#             messagebox.showerror("Error", f"No se pudo analizar el PDF: {e}")
#             # Remove statement if parse fails
#             return
#         # Adjust transaction dates to have correct year/month from metadata
#         adjusted_transactions: List[Transaction] = []
#         for t in transactions:
#             # Replace month and year with provided ones
#             try:
#                 new_date = datetime.date(year, month, t.date.day)
#             except Exception:
#                 # If day is invalid for month, skip transaction
#                 continue
#             adjusted_transactions.append(
#                 Transaction(
#                     date=new_date,
#                     store_name=t.store_name,
#                     amount=t.amount,
#                     installment_number=t.installment_number,
#                 )
#             )
#         if not adjusted_transactions:
#             messagebox.showwarning("Sin datos", "No se encontraron transacciones en el PDF.")
#         else:
#             self.db.add_transactions(statement_id, adjusted_transactions)
#             messagebox.showinfo("Éxito", f"Se cargaron {len(adjusted_transactions)} transacciones.")
#         # Refresh statements list
#         self.refresh_statements()
#
#     def manage_categories(self) -> None:
#         """Open a dialog to add or delete categories."""
#         def refresh_cat_list() -> None:
#             for item in tree.get_children():
#                 tree.delete(item)
#             for cid, name in self.db.get_categories():
#                 tree.insert('', 'end', iid=str(cid), values=(name,))
#
#         # Dialog window
#         dlg = tk.Toplevel(self.root)
#         dlg.title("Gestionar Categorías")
#         dlg.geometry('300x400')
#         dlg.transient(self.root)
#         # Treeview
#         tree = ttk.Treeview(dlg, columns=("nombre",), show="headings")
#         tree.heading("nombre", text="Nombre de categoría")
#         tree.grid(row=0, column=0, columnspan=2, sticky='nsew', padx=5, pady=5)
#         # Scrollbar
#         sb = ttk.Scrollbar(dlg, orient='vertical', command=tree.yview)
#         tree.configure(yscroll=sb.set)
#         sb.grid(row=0, column=2, sticky='ns')
#         dlg.rowconfigure(0, weight=1)
#         dlg.columnconfigure(1, weight=1)
#         # Buttons
#         def add_cat() -> None:
#             name = simpledialog.askstring("Nueva Categoría", "Ingrese el nombre de la categoría")
#             if name:
#                 try:
#                     self.db.add_category(name.strip())
#                     refresh_cat_list()
#                 except ValueError as e:
#                     messagebox.showerror("Error", str(e))
#
#         def delete_cat() -> None:
#             selected = tree.selection()
#             if not selected:
#                 return
#             cid = int(selected[0])
#             if cid == self.db.get_default_category_id():
#                 messagebox.showwarning("Advertencia", "No se puede eliminar la categoría por defecto.")
#                 return
#             confirm = messagebox.askyesno("Confirmar", "¿Está seguro que desea eliminar la categoría seleccionada?")
#             if confirm:
#                 self.db.delete_category(cid)
#                 refresh_cat_list()
#                 # If transactions view is displayed, refresh categories there
#                 selection = self.tree_statements.selection()
#                 if selection:
#                     self.display_transactions(int(selection[0]))
#
#         btn_add = ttk.Button(dlg, text="Agregar", command=add_cat)
#         btn_add.grid(row=1, column=0, padx=5, pdy=5, sticky='ew')
#         btn_del = ttk.Button(dlg, text="Eliminar", command=delete_cat)
#         btn_del.grid(row=1, column=1, padx=5, pady=5, sticky='ew')
#         refresh_cat_list()
#         dlg.grab_set()
#         dlg.wait_window()
#
#     def delete_statement(self) -> None:
#         """Delete the currently selected statement."""
#         selected = self.tree_statements.selection()
#         if not selected:
#             messagebox.showinfo("Eliminar resumen", "Seleccione un resumen para eliminar.")
#             return
#         stmt_id = int(selected[0])
#         # Confirm deletion
#         confirm = messagebox.askyesno("Confirmar eliminación", "¿Está seguro de que desea eliminar el resumen seleccionado y todas sus transacciones?")
#         if not confirm:
#             return
#         # Delete from DB
#         self.db.delete_statement(stmt_id)
#         # Clear transaction table and chart
#         for item in self.tree_transactions.get_children():
#             self.tree_transactions.delete(item)
#         self.ax.clear()
#         self.ax.text(0.5, 0.5, "Sin datos", ha='center', va='center')
#         self.canvas.draw()
#         # Clear summary labels
#         if hasattr(self, 'lbl_total'):
#             self.lbl_total.config(text='')
#         if hasattr(self, 'lbl_cat_totals'):
#             self.lbl_cat_totals.config(text='')
#         # Refresh statements list
#         self.refresh_statements()
#
#     def on_transaction_double_click(self, event) -> None:
#         """Handle double click on transaction row to edit its store category."""
#         item_id = self.tree_transactions.identify_row(event.y)
#         if not item_id:
#             return
#         txn_id = int(item_id)
#         # Retrieve store id from DB
#         # Use hidden column? We stored store id as the sixth element in tuple but not visible.
#         # Re-query to get store id and store name.
#         cur = self.db.conn.cursor()
#         cur.execute(
#             "SELECT store_id FROM transactions WHERE id = ?",
#             (txn_id,),
#         )
#         row = cur.fetchone()
#         if not row:
#             return
#         store_id = row[0]
#         # Fetch list of categories
#         categories = self.db.get_categories()
#         category_names = [name for cid, name in categories]
#         category_ids = [cid for cid, name in categories]
#         current_cid, current_cname = self.db.get_store_category(store_id)
#         # Ask user to select category via simple dialog
#         dlg = tk.Toplevel(self.root)
#         dlg.title("Asignar Categoría")
#         dlg.transient(self.root)
#         ttk.Label(dlg, text="Seleccione una categoría:").grid(row=0, column=0, padx=10, pady=10)
#         combo = ttk.Combobox(dlg, values=category_names, state='readonly')
#         combo.grid(row=1, column=0, padx=10, pady=10)
#         # Set current selection
#         try:
#             idx = category_ids.index(current_cid)
#             combo.current(idx)
#         except ValueError:
#             pass
#         def on_ok() -> None:
#             idx = combo.current()
#             if idx >= 0:
#                 new_cid = category_ids[idx]
#                 self.db.update_store_category(store_id, new_cid)
#                 # Refresh transactions and chart
#                 selection = self.tree_statements.selection()
#                 if selection:
#                     self.display_transactions(int(selection[0]))
#             dlg.destroy()
#         btn_ok = ttk.Button(dlg, text="Aceptar", command=on_ok)
#         btn_ok.grid(row=2, column=0, padx=10, pady=10)
#         dlg.grab_set()
#         dlg.wait_window()
#
#     def run(self) -> None:
#         self.root.mainloop()
#
#
# def main() -> None:
#     if not GUI_AVAILABLE:
#         print("La interfaz gráfica no está disponible en este entorno. El programa no se ejecutará.")
#         return
#     try:
#         app = ExpenseApp()
#         app.run()
#     except RuntimeError as e:
#         print(str(e))
#
#
# if __name__ == '__main__':
#     main()