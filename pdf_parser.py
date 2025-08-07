# pdf_parser.py

import re
import datetime
from typing import List
import fitz  # PyMuPDF
from transaction import Transaction

class PDFParser:
    DATE_PATTERN = re.compile(r"^(\d{1,2}[/-]\d{1,2})")
    MONTH_PATTERN = re.compile(r"^(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúÜü\.]+)")
    AMOUNT_PATTERN = re.compile(r"\d[\d\.]*,\d{2}-?")
    INSTALL_PATTERN = re.compile(r"C\.?\s*(\d{1,2})/(\d{1,2})", re.IGNORECASE)
    MONTH_NAMES = {
        'ene':1,'feb':2,'mar':3,'abr':4,'may':5,'jun':6,
        'jul':7,'ago':8,'sep':9,'oct':10,'nov':11,'dic':12
    }

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
                    d, mn = map(int, part.replace('-', '/').split('/'))
                    date = datetime.date(datetime.date.today().year, mn, d)
                    rest = line[m.end():].strip()
                except:
                    pass

            if date is None:
                m2 = self.MONTH_PATTERN.match(line)
                if m2:
                    d = int(m2.group(1))
                    mn = self.MONTH_NAMES.get(m2.group(2).lower()[:3])
                    if mn:
                        date = datetime.date(datetime.date.today().year, mn, d)
                        rest = line[m2.end():].strip()

            if date is None or not rest:
                continue

            amt_m = None
            for am in self.AMOUNT_PATTERN.finditer(rest):
                amt_m = am
            if not amt_m:
                continue

            amt_str = amt_m.group(0)
            neg = '-' in amt_str or (amt_m.start() > 0 and rest[amt_m.start()-1] == '-')
            clean = amt_str.replace('.', '').replace(',', '.')
            try:
                amt = float(clean)
                if neg: amt = -amt
            except:
                continue

            desc = rest[:amt_m.start()].strip()
            inst = None
            im = self.INSTALL_PATTERN.search(desc)
            if im:
                inst = int(im.group(1))
                desc = desc[:im.start()].strip()
            if '*' in desc:
                desc = desc.split('*',1)[1].strip()

            transactions.append(Transaction(date=date, store_name=desc or orig, amount=amt, installment_number=inst))

        return transactions
