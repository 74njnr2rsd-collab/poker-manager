def money(value):
    return f"{int(value):,}".replace(",", " ")

def calculate_settlement(rows):
    """Минимизирует количество переводов между игроками."""
    balances = []
    for row in rows:
        net = int(row.get("cashouts", 0)) - int(row.get("buys", 0))
        if net > 0:
            balances.append({"name": row["name"], "amount": net, "side": "creditor"})
        elif net < 0:
            balances.append({"name": row["name"], "amount": -net, "side": "debtor"})

    debtors = [{"name": x["name"], "amount": x["amount"]} for x in balances if x["side"] == "debtor"]
    creditors = [{"name": x["name"], "amount": x["amount"]} for x in balances if x["side"] == "creditor"]
    debtors.sort(key=lambda x: x["amount"], reverse=True)
    creditors.sort(key=lambda x: x["amount"], reverse=True)

    result = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i]["amount"], creditors[j]["amount"])
        if amount:
            result.append({"from": debtors[i]["name"], "to": creditors[j]["name"], "amount": amount})
        debtors[i]["amount"] -= amount
        creditors[j]["amount"] -= amount
        if debtors[i]["amount"] == 0:
            i += 1
        if creditors[j]["amount"] == 0:
            j += 1
    return result
