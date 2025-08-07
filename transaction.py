# transaction.py

from dataclasses import dataclass
from typing import Optional
import datetime

@dataclass
class Transaction:
    date: datetime.date
    store_name: str
    amount: float
    installment_number: Optional[int] = None
