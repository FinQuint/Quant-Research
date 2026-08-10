from finquint.fixed_income import Bond, generate_cashflows, price_bond
bond=Bond(100.0,0.05,5,2)
print("Cash flows:", generate_cashflows(bond))
print(f"Bond price: {price_bond(bond,0.04):.4f}")
