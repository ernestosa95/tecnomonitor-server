import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "dashboard_app"))  # para encontrar auth
import database, auth
db = database.SessionLocal()
email = "cliente.test@hospitalitaliano.org.ar"
u = db.query(database.UserModel).filter_by(email=email).first()
if not u:
    u = database.UserModel(email=email,
        hashed_password=auth.get_password_hash("Prueba1234!"),
        full_name="Cliente de Prueba", role="Cliente", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
hid = "PMP2"   # <-- cambialo por un hospital_id real tuyo
if not db.query(database.ClienteHospitalAccess).filter_by(user_id=u.id, hospital_id=hid).first():
    db.add(database.ClienteHospitalAccess(user_id=u.id, hospital_id=hid,
        ver_infra=True, ver_software=False, ver_kpis=True)); db.commit()
print("OK:", auth.hospitales_de_cliente(email, db))
db.close()
