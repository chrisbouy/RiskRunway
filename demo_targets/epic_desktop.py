import tkinter as tk
from tkinter import ttk

root = tk.Tk()
root.title("Applied Epic - Demo")
root.geometry("1440x900")
root.configure(bg="#f0f0f0")

# ===== TOP BAR =====
top_bar = tk.Frame(root, bg="#2f3e7a", height=60)
top_bar.pack(fill="x")

tk.Label(top_bar, text="APPLIED EPIC", fg="white", bg="#2f3e7a",
         font=("Arial", 16, "bold")).pack(side="left", padx=20)

# ===== SIDEBAR =====
sidebar = tk.Frame(root, bg="#e6e6e6", width=200)
sidebar.pack(side="left", fill="y")

menu_items = [
    "Account Detail", "Contacts", "Opportunities",
    "Client Contracts", "Policies", "Proofs of Insurance",
    "Transactions", "Attachments", "Claims", "Activities"
]

for item in menu_items:
    tk.Label(sidebar, text=item, anchor="w",
             bg="#e6e6e6", padx=10).pack(fill="x", pady=2)

# ===== MAIN AREA =====
main = tk.Frame(root, bg="white")
main.pack(side="left", fill="both", expand=True, padx=10, pady=10)

# ===== SECTION: ADD ACCOUNT =====
tk.Label(main, text="Add Account", font=("Arial", 14, "bold"),
         bg="white").pack(anchor="w", pady=10)

form = tk.Frame(main, bg="white")
form.pack(anchor="w")

def field(label, row, col):
    tk.Label(form, text=label, bg="white").grid(row=row, column=col*2, sticky="w", padx=5, pady=5)
    tk.Entry(form, width=25).grid(row=row, column=col*2+1, padx=5, pady=5)

field("First", 0, 0)
field("Middle", 0, 1)
field("Last", 0, 2)

field("Account Name", 1, 0)
field("Agency", 2, 0)
field("Branch", 2, 1)

# ===== ADDRESS BOX =====
tk.Label(main, text="Address", bg="white").pack(anchor="w", pady=(20, 5))

address = tk.Text(main, height=5, width=60)
address.pack(anchor="w")

# ===== PHONE SECTION =====
tk.Label(main, text="Primary Phone", bg="white").pack(anchor="w", pady=(20, 5))

phone_frame = tk.Frame(main, bg="white")
phone_frame.pack(anchor="w")

for i, label in enumerate(["Residence", "Business", "Mobile"]):
    tk.Label(phone_frame, text=label, bg="white").grid(row=i, column=0, sticky="w")
    tk.Entry(phone_frame).grid(row=i, column=1, padx=5, pady=3)

root.mainloop()