import sqlite3
import os

db_path = "macs.db"
if not os.path.exists(db_path):
    print("DB not found.")
else:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    columns = [col[1] for col in c.execute("PRAGMA table_info(paper_trades)").fetchall()]
    
    def add_col(name, dtype):
        if name not in columns:
            try:
                c.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {dtype}")
                print(f"Added {name}")
            except Exception as e:
                print(e)
                
    add_col("proposal_id", "VARCHAR")
    add_col("contract_id", "VARCHAR")
    add_col("result", "VARCHAR")
    add_col("payout", "FLOAT")
    add_col("closed_timestamp", "DATETIME")
    add_col("tech_score", "FLOAT")
    add_col("ai_score", "FLOAT")
    add_col("confidence", "FLOAT")
    add_col("regime", "VARCHAR")
    add_col("error_reason", "VARCHAR")

    conn.commit()
    conn.close()
    print("Done")
