import sys
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QListWidget, QGridLayout, QFrame
)
from PyQt5.QtCore import Qt

app = QApplication(sys.argv)

window = QWidget()
window.setWindowTitle("Applied Epic - Demo")
window.setGeometry(0, 0, 1440, 900)

main_layout = QVBoxLayout(window)
main_layout.setContentsMargins(0, 0, 0, 0)

# ===== TOP BAR =====
top_bar = QFrame()
top_bar.setStyleSheet("background-color: #2f3e7a; color: white;")
top_bar.setFixedHeight(60)

top_layout = QHBoxLayout(top_bar)
top_layout.addWidget(QLabel("APPLIED"))
top_layout.addStretch()

main_layout.addWidget(top_bar)

# ===== BODY =====
body = QHBoxLayout()

# ===== SIDEBAR =====
sidebar = QListWidget()
sidebar.addItems([
    "Account Detail", "Contacts", "Opportunities",
    "Client Contracts", "Policies", "Transactions"
])
sidebar.setFixedWidth(220)
sidebar.setStyleSheet("""
    QListWidget {
        background-color: #e6e6e6;
        border: none;
    }
    QListWidget::item:selected {
        background-color: #2f80ed;
        color: white;
    }
""")

body.addWidget(sidebar)

# ===== MAIN FORM =====
content = QWidget()
content_layout = QVBoxLayout(content)

title = QLabel("Add Account")
title.setStyleSheet("font-size: 18px; font-weight: bold;")
content_layout.addWidget(title)

form = QGridLayout()

def input_field(label, row, col):
    lbl = QLabel(label)
    inp = QLineEdit()
    inp.setStyleSheet("border: 1px solid #ccc; padding: 4px;")
    form.addWidget(lbl, row, col)
    form.addWidget(inp, row, col + 1)

input_field("First", 0, 0)
input_field("Middle", 0, 2)
input_field("Last", 0, 4)

# Dropdown
form.addWidget(QLabel("Agency"), 2, 0)
agency = QComboBox()
agency.addItems(["ASN", "ABC", "XYZ"])
form.addWidget(agency, 2, 1)

content_layout.addLayout(form)

body.addWidget(content)
main_layout.addLayout(body)

window.show()
sys.exit(app.exec_())