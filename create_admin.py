# create_users.py
from database import SessionLocal, UserModel
from passlib.context import CryptContext

# Configuración de seguridad para el hash de contraseñas
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def crear_multiples_usuarios():
    db = SessionLocal()
    
    # LISTA DE USUARIOS A CREAR
    # Puedes agregar o quitar de esta lista según necesites
    usuarios_nuevos = [
        {"email": "stefania.mendez@tecnoimagen.com.ar", "name": "Stefania Mendez"},
        {"email": "yesica.perez@tecnoimagen.com.ar", "name": "Yesica Perez"},
        {"email": "giselle.santarelli@tecnoimagen.com.ar", "name": "Giselle Santarelli"},
        {"email": "sofia.sanchez@tecnoimagen.com.ar", "name": "Sofia Sanchez"},
    ]
    
    password_comun = "Tecno2026." # Contraseña temporal para todos

    print(f"🚀 Iniciando creación de {len(usuarios_nuevos)} usuarios de Ingeniería...\n")

    for u in usuarios_nuevos:
        email = u["email"].lower().strip()
        nombre = u["name"]

        # 1. Validación de dominio (Seguridad extra)
        if not email.endswith("@tecnoimagen.com.ar"):
            print(f"❌ Saltado: {email} no pertenece al dominio @tecnoimagen.com.ar")
            continue

        # 2. Verificamos si ya existe
        user_exists = db.query(UserModel).filter(UserModel.email == email).first()
        if user_exists:
            print(f"⚠️  El usuario {email} ya existe. No se realizaron cambios.")
            continue

        # 3. Creamos el registro
        hashed_pw = pwd_context.hash(password_comun)
        nuevo_usuario = UserModel(
            email=email,
            hashed_password=hashed_pw,
            full_name=nombre,
            role="Ingenieria",  # Perfil solicitado
            is_active=True
        )

        try:
            db.add(nuevo_usuario)
            db.commit()
            print(f"✅ Creado: {nombre} ({email}) - Rol: Ingenieria")
        except Exception as e:
            db.rollback()
            print(f"❌ Error creando a {nombre}: {e}")

    db.close()
    print("\n✨ Proceso finalizado.")

if __name__ == "__main__":
    crear_multiples_usuarios()