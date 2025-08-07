# app.py

#!/usr/bin/env python3
import os
import re
import datetime
import pandas as pd
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from database_manager import DatabaseManager
from pdf_parser import PDFParser
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

class ExpenseApp:
    def __init__(self) -> None:
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
        for i in range(9):
            top.columnconfigure(i, weight=0)
        top.columnconfigure(8, weight=1)

        ttk.Button(top, text="Cargar Resumen",     command=self.load_statement).grid(row=0, column=0, padx=5)
        ttk.Button(top, text="Gestionar Categorías", command=self.manage_categories).grid(row=0, column=1, padx=5)
        ttk.Button(top, text="Modificar Resumen",  command=self.modify_statement_ui).grid(row=0, column=2, padx=5)
        ttk.Button(top, text="Eliminar Resumen",   command=self.delete_statement).grid(row=0, column=3, padx=5)
        ttk.Button(top, text="Exportar Excel",     command=self.export_all_to_excel).grid(row=0, column=4, padx=5)
        ttk.Button(top, text="Añadir Gasto",       command=self.add_manual_transaction_ui).grid(row=0, column=5, padx=5)
        ttk.Button(top, text="Eliminar Gasto",     command=self.delete_manual_transaction_ui).grid(row=0, column=6, padx=5)
        self.show_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Todos períodos", variable=self.show_all_var, command=self.on_toggle_mode).grid(row=0, column=7, padx=5)
        ttk.Label(top, text="Seleccione un resumen para ver detalles").grid(row=0, column=8, padx=10, sticky='w')

    def _build_statement_list(self, parent):
        f = ttk.Frame(parent)
        f.grid(row=1, column=0, sticky='nsew', padx=5, pady=5)
        f.columnconfigure(0, weight=1); f.rowconfigure(0, weight=1)

        self.tree_statements = ttk.Treeview(f, columns=("mes","año","tarjeta"), show='headings')
        for c,t in zip(("mes","año","tarjeta"),("Mes","Año","Tarjeta")):
            self.tree_statements.heading(c, text=t)
        self.tree_statements.bind("<<TreeviewSelect>>", self.on_statement_select)
        self.tree_statements.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(f, orient='vertical', command=self.tree_statements.yview)
        self.tree_statements.configure(yscroll=sb.set)
        sb.grid(row=0, column=1, sticky='ns')

    def _build_transactions_view(self, parent):
        f = ttk.Frame(parent)
        f.grid(row=2, column=0, sticky='nsew', padx=5, pady=5)
        f.columnconfigure(0, weight=3); f.columnconfigure(1, weight=2); f.columnconfigure(2, weight=3)
        f.rowconfigure(0, weight=1)

        self.tree_transactions = ttk.Treeview(f, columns=("fecha","tienda","monto","cuota","categoria"), show='headings')
        for c,t in zip(("fecha","tienda","monto","cuota","categoria"),("Fecha","Tienda","Monto","Cuota","Categoría")):
            self.tree_transactions.heading(c, text=t)
        self.tree_transactions.bind("<Double-1>", self.on_transaction_double_click)
        self.tree_transactions.grid(row=0, column=0, sticky='nsew')
        sb2 = ttk.Scrollbar(f, orient='vertical', command=self.tree_transactions.yview)
        self.tree_transactions.configure(yscroll=sb2.set); sb2.grid(row=0, column=1, sticky='ns')

        self.figure = plt.Figure(figsize=(6,6))
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=f)
        self.canvas.get_tk_widget().grid(row=0, column=2, sticky='nsew', padx=10)

        sf = ttk.Frame(f)
        sf.grid(row=1, column=0, columnspan=3, sticky='ew', pady=5)
        sf.columnconfigure(0, weight=3); sf.columnconfigure(1, weight=2)

        self.lbl_total = ttk.Label(sf, text='', font=(None,16,'bold'))
        self.lbl_total.grid(row=0, column=0, sticky='w')
        self.lbl_cat_tot = ttk.Label(sf, text='', justify='left', font=(None,12))
        self.lbl_cat_tot.grid(row=0, column=1, sticky='w')

    def refresh_statements(self) -> None:
        for i in self.tree_statements.get_children():
            self.tree_statements.delete(i)
        for sid, m, y, cn, _ in self.db.get_statements():
            self.tree_statements.insert('', 'end', iid=str(sid), values=(f"{m:02d}", y, cn))

    def on_statement_select(self, event):
        sel = self.tree_statements.selection()
        if sel:
            self.display_transactions(int(sel[0]))

    def display_transactions(self, sid: int):
        for i in self.tree_transactions.get_children():
            self.tree_transactions.delete(i)
        for tid, date_iso, store, amt, inst, sid2 in self.db.get_transactions_by_statement(sid):
            cid, cname = self.db.get_store_category(sid2)
            self.tree_transactions.insert('', 'end', iid=str(tid), values=(
                datetime.datetime.fromisoformat(date_iso).strftime('%d/%m/%Y'),
                store, f"{amt:,.2f}", inst or '', cname
            ))
        self.update_chart(sid)

    def update_chart(self, sid: int):
        self.ax.clear()
        sums = self.db.get_all_category_sums() if self.show_all_var.get() else self.db.get_category_sums(sid)
        positive = [(n, t) for n, t in sums if t>0 and n.upper()!='NO APLICA']
        if positive:
            labels, sizes = zip(*positive)
            self.ax.pie(sizes,
                        labels=labels,
                        autopct='%1.1f%%',
                        startangle=90,
                        textprops={'fontsize':12})
            self.ax.axis('equal')
            self.ax.set_title('Gastos por Categoría', fontsize=18)
        else:
            self.ax.text(0.5,0.5,'Sin datos', ha='center', va='center')
        self.canvas.draw()

        total = sum(t for _,t in sums)
        self.lbl_total.config(text=f"Total: $ {total:,.2f}")
        self.lbl_cat_tot.config(text='\n'.join(f"{n}: $ {t:,.2f}" for n, t in sums))

    def add_manual_transaction_ui(self):
        sel = self.tree_statements.selection()
        if not sel or int(sel[0]) != self.cash_id:
            messagebox.showinfo('Añadir Gasto','Seleccione el período EFECTIVO.')
            return
        sid = self.cash_id
        date = datetime.date.today()
        store = simpledialog.askstring('Comercio','Nombre del comercio:')
        if not store: return
        amt_str = simpledialog.askstring('Monto','Monto positivo:')
        try:
            amt = float(amt_str.replace(',','.'))
        except:
            messagebox.showerror('Error','Monto inválido')
            return
        self.db.add_manual_transaction(sid, date, store, amt)
        self.display_transactions(sid)
        messagebox.showinfo('Añadir Gasto','Gasto agregado')

    def delete_manual_transaction_ui(self):
        sel = self.tree_transactions.selection()
        if not sel:
            messagebox.showinfo('Eliminar Gasto','Seleccione un gasto')
            return
        txn_id = int(sel[0])
        if not messagebox.askyesno('Confirmar','¿Eliminar este gasto?'):
            return
        self.db.conn.execute('DELETE FROM transactions WHERE id=?',(txn_id,))
        self.db.conn.commit()
        sel_stmt = self.tree_statements.selection()
        if sel_stmt:
            self.display_transactions(int(sel_stmt[0]))
        messagebox.showinfo('Eliminar Gasto','Gasto eliminado')

    def on_transaction_double_click(self, event):
        item = self.tree_transactions.identify_row(event.y)
        if not item: return
        txn_id = int(item)
        cur = self.db.conn.cursor()
        cur.execute('SELECT store_id FROM transactions WHERE id=?',(txn_id,))
        row = cur.fetchone()
        if not row: return
        store_id = row[0]
        cats = self.db.get_categories()
        names = [n for _,n in cats]; ids = [i for i,_ in cats]
        curr_id,_ = self.db.get_store_category(store_id)

        dlg = tk.Toplevel(self.root)
        dlg.title('Asignar Categoría'); dlg.transient(self.root)
        ttk.Label(dlg, text='Seleccione categoría:').grid(row=0,column=0,padx=10,pady=10)
        combo = ttk.Combobox(dlg, values=names, state='readonly'); combo.grid(row=1,column=0,padx=10,pady=10)
        try: combo.current(ids.index(curr_id))
        except: pass

        def on_ok():
            idx = combo.current()
            if idx>=0:
                self.db.update_store_category(store_id, ids[idx])
                sel = self.tree_statements.selection()
                if sel: self.display_transactions(int(sel[0]))
            dlg.destroy()

        ttk.Button(dlg, text='Aceptar', command=on_ok).grid(row=2,column=0,padx=10,pady=10)
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
                adjusted.append(t.__class__(date=nd, store_name=t.store_name, amount=t.amount, installment_number=t.installment_number))
            except:
                continue

        if not adjusted:
            messagebox.showwarning('Sin datos','No se encontraron transacciones en el PDF')
        else:
            self.db.add_transactions(sid, adjusted)
            messagebox.showinfo('Éxito', f'Se cargaron {len(adjusted)} transacciones')

        self.refresh_statements()

    def modify_statement_ui(self):
        sel = self.tree_statements.selection()
        if not sel:
            messagebox.showinfo('Modificar resumen','Seleccione un resumen para modificar')
            return
        sid = int(sel[0])
        # obtener valores actuales
        current = next((row for row in self.db.get_statements() if row[0]==sid), None)
        if not current: return
        _, month, year, card_name, _ = current

        ms = simpledialog.askstring('Mes','Ingrese el mes (1-12)', initialvalue=str(month))
        if not ms: return
        ys = simpledialog.askstring('Año','Ingrese el año', initialvalue=str(year))
        if not ys: return
        cn = simpledialog.askstring('Nombre de Tarjeta','Ingrese el nombre de la tarjeta', initialvalue=card_name)
        if not cn: return

        try:
            new_month, new_year = int(ms), int(ys)
        except:
            messagebox.showerror('Error','Mes o año inválido')
            return

        try:
            c = self.db.conn.cursor()
            c.execute('UPDATE statements SET month=?, year=?, card_name=? WHERE id=?',
                      (new_month, new_year, cn.strip(), sid))
            self.db.conn.commit()
            messagebox.showinfo('Modificar resumen','Resumen modificado correctamente')
            self.refresh_statements()
        except Exception as e:
            messagebox.showerror('Error', str(e))

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
            for cid, n in self.db.get_categories():
                tree.insert('', 'end', iid=str(cid), values=(n,))

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

    def on_toggle_mode(self):
        self.on_statement_select(None)

    def run(self):
        self.root.mainloop()

if __name__ == '__main__':
    ExpenseApp().run()
