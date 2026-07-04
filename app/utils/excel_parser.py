import os
import pandas as pd
from app.utils.csv_parser import normalize_csv_row


def read_reviews_file(upload_path):
    extension = os.path.splitext(upload_path)[1].lower()

    if extension in {".xlsx", ".xls"}:
        return pd.read_excel(upload_path)

    raise ValueError("Unsupported file type. Please upload an Excel file.")


def iter_normalized_rows(df):
    for _, row in df.iterrows():
        yield normalize_csv_row(row)
