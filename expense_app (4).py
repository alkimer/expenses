#!/usr/bin/env python3
"""
Expense Manager Application
==========================

This module implements a desktop application for tracking and categorising
credit-card expenses. It reads PDF statements using PyMuPDF, extracts transactions,
persists them in a SQLite database, allows manual entry for cash, deletion of
manual entries, and presents interactive summary charts, including per-period and
all-period views.
"""

import os
import re
import sqlite3
import datetime
import pandas as pd
from dataclasses import dataclass
from typing import List, Optional, Tuple

# GUI and plotting
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

# PDF extraction
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

@dataclass
class Transaction:
    date: datetime.date
    store_name: str
    amount: float
    installment_number: Optional[int] = None

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
            "SELECT id, month, year, card_name, last4digits FROM statements ORDER BY year DESC, month DESC, card_name"
        )
        return c.fetchall()

    # Transaction operations
    def add_transactions(self, sid: int, txs: List[Transaction]) -> None:
        c = self.conn.cursor()
        for t in txs:
            store_id = self.get_store_id(t.store_name)
            c.execute(
                "INSERT INTO transactions(statement_id,date,store_id,amount,installment_number) VALUES(?,?,?,?,?)",
                (sid, t.date.isoformat(), store_id, t.amount, t.installment_number)
            )
        self.conn.commit()

    def get_transactions_by_statement(self, sid: int) -> List[Tuple[int, str, str, float, Optional[int], int]]:
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
            "INSERT INTO transactions(statement_id,date,store_id,amount,installment_number) VALUES(?,?,?,?,NULL)",
            (sid, date.isoformat(), store_id, amt)
        )
        self.conn.commit()

class PDFParser:
    DATE_PATTERN = re.compile(r"^(\d{1,2}[/-]\d{1,2})")
    MONTH_PATTERN = re.compile(r"^(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúÜü\.]+)")
    AMOUNT_PATTERN = re.compile(r"\d[\d\.]*,\d{2}-?")
    INSTALL_PATTERN = re.compile(r"C\.?\s*(\d{1,2})/(\d{1,2})", re.IGNORECASE)
    MONTH_NAMES = { 'ene':1,'feb':2,'mar':3,'abr':4,'may':5,'jun':6,'jul':7,'ago':8,'sep':9,'oct':10,'nov':11,'dic':12 }

    def __init__(self) -> None:
        if not PYMUPDF_AVAILABLE:
            raise RuntimeError("PyMuPDF no está disponible.")

    def parse_pdf(self, pdf_path: str) -> List[Transaction]:
        doc = fitz.open(pdf_path)
        lines: List[str] = []
        for page in doc:
            lines.extend(page.get_text("text").split("\n"))
        doc.close()
        transactions: List[Transaction] = []
        for line in lines:
            orig = line; line = line.strip()
            if not line: continue
            date = None; rest = None
            m = self.DATE_PATTERN.match(line)
            if m:
                part = m.group(1)
                try:
                    d,mn = map(int, part.replace('-', '/').split('/'))
                    date = datetime.date(datetime.date.today().year, mn, d)
                    rest = line[m.end():].strip()
                except: pass
            if date is None:
                m2 = self.MONTH_PATTERN.match(line)
                if m2:
                    d = int(m2.group(1))
                    mn = self.MONTH_NAMES.get(m2.group(2).lower()[:3])
                    if mn:
                        date = datetime.date(datetime.date.today().year, mn, d)
                        rest = line[m2.end():].strip()
            if date is None or not rest: continue
            amt_m = None
            for am in self.AMOUNT_PATTERN.finditer(rest): amt_m = am
            if not amt_m: continue
            amt_str = amt_m.group(0)
            neg = '-' in amt_str or (amt_m.start()>0 and rest[amt_m.start()-1]=='-')
            clean = amt_str.replace('.', '').replace(',', '.')
            try:
                amt = float(clean)
                if neg: amt = -amt
            except: continue
            desc = rest[:amt_m.start()].strip()
            inst = None
            im = self.INSTALL_PATTERN.search(desc)
            if im:
                inst = int(im.group(1)); desc = desc[:im.start()].strip()
            if '*' in desc: desc = desc.split('*',1)[1].strip()
            transactions.append(Transaction(date=date, store_name=desc or orig, amount=amt, installment_number=inst))
        return transactions

class ExpenseApp:
    def __init__(self) -> None:
        if not GUI_AVAILABLE or not MATPLOTLIB_AVAILABLE:
            raise RuntimeError("Entorno GUI incompleto.")
        self.db = DatabaseManager()
        self.cash_id = self.db.get_cash_statement_id()
        self.parser = PDFParser()
        self.root = tk.Tk()
        self.root.title("Gestor de Gastos de Tarjetas")
        self.root.geometry('1000x700')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame = ttk.Frame(self.root)
        frame.grid(row=0, column=0, sticky='nsew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self._build_top_controls(frame)
        self._build_statement_list(frame)
        self._build_transactions_view(frame)
        self.refresh_statements()

    def _build_top_controls(self, parent):
        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky='ew', pady=5, padx=5)
        top.columnconfigure(7, weight=1)
        ttk.Button(top, text="Cargar Resumen", command=self.load_statement).grid(row=0,column=0,padx=5)
        ttk.Button(top, text="Gestionar Categorías", command=self.manage_categories).grid(row=0,column=1,padx=5)
        ttk.Button(top, text="Eliminar Resumen", command=self.delete_statement).grid(row=0,column=2,padx=5)
        ttk.Button(top, text="Exportar Excel", command=self.export_all_to_excel).grid(row=0,column=3,padx=5)
        ttk.Button(top, text="Añadir Gasto", command=self.add_manual_transaction_ui).grid(row=0,column=4,padx=5)
        ttk.Button(top, text="Eliminar Gasto", command=self.delete_manual_transaction_ui).grid(row=0,column=5,padx=5)
        self.show_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Todos períodos", variable=self.show_all_var, command=self.on_toggle_mode).grid(row=0,column=6,padx=5)
        ttk.Label(top, text="Seleccione un resumen para ver detalles").grid(row=0,column=7,padx=10,sticky='w')

    def _build_statement_list(self, parent):
        f = ttk.Frame(parent)
        f.grid(row=1,column=0,sticky='nsew',padx=5,pady=5)
        f.columnconfigure(0,weight=1); f.rowconfigure(0,weight=1)
        self.tree_statements = ttk.Treeview(f,columns=("mes","año","tarjeta"),show='headings')
        for c,t in zip(("mes","año","tarjeta"),("Mes","Año","Tarjeta")):
            self.tree_statements.heading(c,text=t)
        self.tree_statements.bind("<<TreeviewSelect>>",self.on_statement_select)
        self.tree_statements.grid(row=0,column=0,sticky='nsew')
        sb=ttk.Scrollbar(f,orient='vertical',command=self.tree_statements.yview)
        self.tree_statements.configure(yscroll=sb.set)
        sb.grid(row=0,column=1,sticky='ns')

    def _build_transactions_view(self, parent):
        f = ttk.Frame(parent)
        f.grid(row=2,column=0,sticky='nsew',padx=5,pady=5)
        f.columnconfigure(0,weight=3); f.columnconfigure(1,weight=2); f.rowconfigure(0,weight=1)
        self.tree_transactions = ttk.Treeview(f,columns=("fecha","tienda","monto","cuota","categoria"),show='headings')
        for c,t in zip(("fecha","tienda","monto","cuota","categoria"),("Fecha","Tienda","Monto","Cuota","Categoría")):
            self.tree_transactions.heading(c,text=t)
        self.tree_transactions.bind("<Double-1>",self.on_transaction_double_click)
        self.tree_transactions.grid(row=0,column=0,sticky='nsew')
        sb2=ttk.Scrollbar(f,orient='vertical',command=self.tree_transactions.yview)
        self.tree_transactions.configure(yscroll=sb2.set); sb2.grid(row=0,column=1,sticky='ns')
        self.figure=plt.Figure(figsize=(4,4))
        self.ax=self.figure.add_subplot(111)
        self.canvas=FigureCanvasTkAgg(self.figure,master=f)
        self.canvas.get_tk_widget().grid(row=0,column=2,sticky='nsew',padx=10)
        sf=ttk.Frame(f); sf.grid(row=1,column=0,columnspan=3,sticky='ew',pady=5)
        sf.columnconfigure(0,weight=3); sf.columnconfigure(1,weight=2)
        self.lbl_total=ttk.Label(sf,text='',font=(None,10,'bold')); self.lbl_total.grid(row=0,column=0,sticky='w')
        self.lbl_cat_tot=ttk.Label(sf,text='',justify='left'); self.lbl_cat_tot.grid(row=0,column=1,sticky='w')

    def refresh_statements(self) -> None:
        for i in self.tree_statements.get_children(): self.tree_statements.delete(i)
        for sid,m,y,cn,_ in self.db.get_statements():
            self.tree_statements.insert('', 'end', iid=str(sid), values=(f"{m:02d}",y,cn))

    def on_statement_select(self,event):
        sel=self.tree_statements.selection()
        if sel: self.display_transactions(int(sel[0]))

    def display_transactions(self,sid:int):
        for i in self.tree_transactions.get_children(): self.tree_transactions.delete(i)
        for tid,date_iso,store,amt,inst,sid2 in self.db.get_transactions_by_statement(sid):
            cid,cname=self.db.get_store_category(sid2)
            self.tree_transactions.insert('', 'end', iid=str(tid), values=(
                datetime.datetime.fromisoformat(date_iso).strftime('%d/%m/%Y'), store, f"{amt:,.2f}", inst or '', cname
            ))
        self.update_chart(sid)

    def update_chart(self,sid:int):
        self.ax.clear()
        sums = self.db.get_all_category_sums() if self.show_all_var.get() else self.db.get_category_sums(sid)
        positive=[(n,t) for n,t in sums if t>0 and n.upper()!='NO APLICA']
        if positive:
            labels,sizes=zip(*positive)
            self.ax.pie(sizes,labels=labels,autopct='%1.1f%%',startangle=90)
            self.ax.axis('equal')
            self.figure.suptitle('Gastos por Categoría')
        else:
            self.ax.text(0.5,0.5,'Sin datos',ha='center',va='center')
        self.canvas.draw()
        total=sum(t for _,t in sums)
        self.lbl_total.config(text=f"Total: $ {total:,.2f}")
        self.lbl_cat_tot.config(text='\n'.join(f"{n}: $ {t:,.2f}" for n,t in sums))

    def add_manual_transaction_ui(self):
        sel=self.tree_statements.selection()
        if not sel or int(sel[0])!=self.cash_id:
            messagebox.showinfo('Añadir Gasto','Seleccione el período EFECTIVO.')
            return
        sid=self.cash_id
        date_str=simpledialog.askstring('Fecha','DD/MM/AAAA',initialvalue='01/01/1900')
        try:
            date=datetime.datetime.strptime(date_str,'%d/%m/%Y').date()
        except:
            messagebox.showerror('Error','Fecha inválida')
            return
        store=simpledialog.askstring('Comercio','Nombre del comercio:')
        if not store: return
        amt_str=simpledialog.askstring('Monto','Monto positivo:')
        try:
            amt=float(amt_str.replace(',','.'))
        except:
            messagebox.showerror('Error','Monto inválido')
            return
        self.db.add_manual_transaction(sid,date,store,amt)
        self.display_transactions(sid)
        messagebox.showinfo('Añadir Gasto','Gasto agregado')

    def delete_manual_transaction_ui(self):
        sel=self.tree_transactions.selection()
        if not sel:
            messagebox.showinfo('Eliminar Gasto','Seleccione un gasto')
            return
        txn_id=int(sel[0])
        if not messagebox.askyesno('Confirmar','¿Eliminar este gasto?'): return
        self.db.conn.execute('DELETE FROM transactions WHERE id=?',(txn_id,))
        self.db.conn.commit()
        sel_stmt=self.tree_statements.selection()
        if sel_stmt:
            self.display_transactions(int(sel_stmt[0]))
        messagebox.showinfo('Eliminar Gasto','Gasto eliminado')

    def on_transaction_double_click(self,event):
        item=self.tree_transactions.identify_row(event.y)
        if not item: return
        txn_id=int(item)
        cur=self.db.conn.cursor()
        cur.execute('SELECT store_id FROM transactions WHERE id=?',(txn_id,))
        row=cur.fetchone()
        if not row: return
        store_id=row[0]
        cats=self.db.get_categories()
        names=[n for _,n in cats]; ids=[i for i,_ in cats]
        curr_id,_=self.db.get_store_category(store_id)
        dlg=tk.Toplevel(self.root); dlg.title('Asignar Categoría'); dlg.transient(self.root)
        ttk.Label(dlg,text='Seleccione categoría:').grid(row=0,column=0,padx=10,pady=10)
        combo=ttk.Combobox(dlg,values=names,state='readonly'); combo.grid(row=1,column=0,padx=10,pady=10)
        try: combo.current(ids.index(curr_id))
        except: pass
        def on_ok():
            idx=combo.current()
            if idx>=0:
                self.db.update_store_category(store_id,ids[idx])
                sel=self.tree_statements.selection()
                if sel: self.display_transactions(int(sel[0]))
            dlg.destroy()
        ttk.Button(dlg,text='Aceptar',command=on_ok).grid(row=2,column=0,padx=10,pady=10)
        dlg.grab_set(); dlg.wait_window()

    def on_toggle_mode(self):
        self.on_statement_select(None)

    def load_statement(self):
        file_path = filedialog.askopenfilename(filetypes=[('PDF files','*.pdf')])
        if not file_path: return
        filename = os.path.basename(file_path)
        default_month, default_year = datetime.date.today().month, datetime.date.today().year
        m = re.search(r"(\d{4})[ -_]?(\d{2})", filename)
        if m:
            default_year, default_month = int(m.group(1)), int(m.group(2))
        ms = simpledialog.askstring('Mes','Ingrese el mes (1-12)', initialvalue=f"{default_month:02d}")
        if not ms: return
        ys = simpledialog.askstring('Año','Ingrese el año', initialvalue=str(default_year))
        if not ys: return
        cn = simpledialog.askstring('Nombre de Tarjeta','Ingrese el nombre de la tarjeta')
        if not cn: return
        try:
            month, year = int(ms), int(ys)
        except:
            messagebox.showerror('Error','Mes o año inválido')
            return
        try:
            sid = self.db.add_statement(month, year, cn.strip(), '', file_path)
        except ValueError as e:
            messagebox.showerror('Error', str(e))
            return
        try:
            txs = self.parser.parse_pdf(file_path)
        except Exception as e:
            messagebox.showerror('Error', f'No se pudo analizar el PDF: {e}')
            return
        adjusted = []
        for t in txs:
            try:
                nd = datetime.date(year, month, t.date.day)
                adjusted.append(Transaction(nd, t.store_name, t.amount, t.installment_number))
            except:
                continue
        if not adjusted:
            messagebox.showwarning('Sin datos','No se encontraron transacciones en el PDF')
        else:
            self.db.add_transactions(sid, adjusted)
            messagebox.showinfo('Éxito', f'Se cargaron {len(adjusted)} transacciones')
        self.refresh_statements()

    def manage_categories(self):
        dlg = tk.Toplevel(self.root)
        dlg.title('Gestionar Categorías')
        dlg.geometry('300x400')
        dlg.transient(self.root)
        tree = ttk.Treeview(dlg, columns=('nombre',), show='headings')
        tree.heading('nombre', text='Nombre de categoría')
        tree.grid(row=0, column=0, columnspan=2, sticky='nsew', padx=5, pady=5)
        sb = ttk.Scrollbar(dlg, orient='vertical', command=tree.yview)
        tree.configure(yscroll=sb.set)
        sb.grid(row=0, column=2, sticky='ns')
        dlg.rowconfigure(0, weight=1); dlg.columnconfigure(1, weight=1)
        def refresh_list():
            for i in tree.get_children(): tree.delete(i)
            for cid, n in self.db.get_categories(): tree.insert('', 'end', iid=str(cid), values=(n,))
        def add_cat():
            n = simpledialog.askstring('Nueva Categoría','Ingrese el nombre')
            if n:
                try:
                    self.db.add_category(n.strip())
                    refresh_list()
                except ValueError as e:
                    messagebox.showerror('Error', str(e))
        def del_cat():
            sel = tree.selection()
            if sel and int(sel[0]) != self.db.get_default_category_id():
                if messagebox.askyesno('Confirmar','¿Eliminar categoría?'):
                    try:
                        self.db.delete_category(int(sel[0]))
                        refresh_list()
                    except ValueError as e:
                        messagebox.showerror('Error', str(e))
        ttk.Button(dlg, text='Agregar', command=add_cat).grid(row=1, column=0, sticky='ew', padx=5, pady=5)
        ttk.Button(dlg, text='Eliminar', command=del_cat).grid(row=1, column=1, sticky='ew', padx=5, pady=5)
        refresh_list(); dlg.grab_set(); dlg.wait_window()

    def delete_statement(self):
        sel = self.tree_statements.selection()
        if not sel:
            messagebox.showinfo('Eliminar resumen','Seleccione un resumen para eliminar')
            return
        sid = int(sel[0])
        if not messagebox.askyesno('Confirmar','¿Eliminar resumen y transacciones?'): return
        self.db.conn.execute('DELETE FROM statements WHERE id=?', (sid,))
        self.db.conn.commit()
        self.refresh_statements()
        self.tree_transactions.delete(*self.tree_transactions.get_children())
        self.ax.clear(); self.ax.text(0.5,0.5,'Sin datos',ha='center',va='center'); self.canvas.draw()
        self.lbl_total.config(text=''); self.lbl_cat_tot.config(text='')

    def export_all_to_excel(self):
        query = (
            "SELECT stm.year AS Año, stm.month AS Mes, stm.card_name AS Tarjeta, "
            "t.date AS Fecha, s.name AS Comercio, t.amount AS Monto, c.name AS Categoría "
            "FROM transactions t "
            "JOIN statements stm ON t.statement_id=stm.id "
            "JOIN stores s ON t.store_id=s.id "
            "JOIN categories c ON s.category_id=c.id "
            "ORDER BY stm.year DESC, stm.month DESC, stm.card_name, t.date"
        )
        df = pd.read_sql_query(query, self.db.conn)
        path = filedialog.asksaveasfilename(defaultextension='.xlsx', filetypes=[('Excel','*.xlsx')])
        if not path: return
        try:
            df.to_excel(path, index=False, sheet_name='Gastos')
            messagebox.showinfo('Excel','Reporte guardado en ' + path)
        except Exception as e:
            messagebox.showerror('Error', str(e))

    def run(self):
        self.root.mainloop()

if __name__ == '__main__':
    ExpenseApp().run()
