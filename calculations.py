def money(value):
    return f"{int(value):,}".replace(",", " ")


def calculate_settlement(rows):
    """Greedy minimum-transfer settlement for a balanced session."""
    creditors = []
    debtors = []
    for row in rows:
        net = int(row["exits"]) - int(row["buys"])
        if net > 0:
            creditors.append({"name": row["name"], "amount": net})
        elif net < 0:
            debtors.append({"name": row["name"], "amount": -net})

    creditors.sort(key=lambda x: x["amount"], reverse=True)
    debtors.sort(key=lambda x: x["amount"], reverse=True)
    result = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i]["amount"], creditors[j]["amount"])
        if amount:
            result.append({
                "from": debtors[i]["name"],
                "to": creditors[j]["name"],
                "amount": amount
            })
        debtors[i]["amount"] -= amount
        creditors[j]["amount"] -= amount
        if debtors[i]["amount"] == 0:
            i += 1
        if creditors[j]["amount"] == 0:
            j += 1
    return result
