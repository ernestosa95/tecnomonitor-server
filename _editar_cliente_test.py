import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "dashboard_app"))  # para encontrar auth
import database, auth
db = database.SessionLocal()

email = "cliente.test@hospitalitaliano.org.ar"

# ============================================================
# CONFIGURÁ ACÁ los hospitales y pestañas que querés dejar.
# Esta lista es la "fuente de verdad": el script deja al cliente
# EXACTAMENTE con estos accesos (agrega los nuevos, actualiza los
# existentes y borra los que ya no figuren).
# ============================================================
ACCESOS = [
    {"hospital_id": "PMP1", "infra": True,  "software": True,  "kpis": True},
    {"hospital_id": "PMP2",   "infra": True,  "software": False, "kpis": True},
]
# ============================================================

u = db.query(database.UserModel).filter_by(email=email).first()
if not u:
    print("❌ No existe el usuario", email); db.close(); raise SystemExit

ids_deseados = {a["hospital_id"] for a in ACCESOS}

# 1) Agregar o actualizar
for a in ACCESOS:
    acc = db.query(database.ClienteHospitalAccess).filter_by(
        user_id=u.id, hospital_id=a["hospital_id"]).first()
    if acc:
        acc.ver_infra, acc.ver_software, acc.ver_kpis = a["infra"], a["software"], a["kpis"]
        print("✏️  actualizado", a["hospital_id"])
    else:
        db.add(database.ClienteHospitalAccess(
            user_id=u.id, hospital_id=a["hospital_id"],
            ver_infra=a["infra"], ver_software=a["software"], ver_kpis=a["kpis"]))
        print("➕ agregado   ", a["hospital_id"])

# 2) Borrar los que ya no están en la lista
for acc in db.query(database.ClienteHospitalAccess).filter_by(user_id=u.id).all():
    if acc.hospital_id not in ids_deseados:
        db.delete(acc); print("➖ quitado    ", acc.hospital_id)

db.commit()
print("\n✅ Estado final:", auth.hospitales_de_cliente(email, db))
db.close()
