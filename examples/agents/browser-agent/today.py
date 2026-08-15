import datetime
d = datetime.date.today()
print(d.isoformat())
print(f"{d.day} {d.strftime('%B')}")
print(d.strftime('%d/%m'))
