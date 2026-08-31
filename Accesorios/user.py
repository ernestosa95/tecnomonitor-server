from database import SessionLocal, UserModel
from passlib.context import CryptContext

# Configuración estándar para encriptar contraseñas (usualmente ya la tienes en auth.py o similar)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password):
    return pwd_context.hash(password)

def actualizar_dario_decision_lider():
    db = SessionLocal()
    try:
        # 1. Buscamos al usuario por su email de forma eficiente
        usuario = db.query(UserModel).filter(UserModel.email == "dario.corderons@tecnoimagen.com.ar").first()
        
        if usuario:
            # 2. Actualizamos el rol al nuevo perfil
            usuario.role = "decision_lider"
            
            # 3. Encriptamos y asignamos la nueva contraseña por seguridad
            usuario.hashed_password = get_password_hash("dco.123")
            
            # 4. Activamos la cuenta (corrigiendo el estado 'Inactivo' de la imagen)
            usuario.is_active = True 
            
            # 5. Forzamos un cambio de clave en el primer login si tu sistema lo soporta (Opcional pero recomendado)
            usuario.must_change_password = False 
            
            # Guardamos los cambios
            db.commit()
            print("Éxito: Usuario dario.corderons actualizado a 'decision_lider', activado y con nueva contraseña.")
        else:
            print("Error: No se encontró al usuario con ese email en la base de datos.")
            
    except Exception as e:
        db.rollback()
        print(f"Error al actualizar la base de datos: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    actualizar_dario_decision_lider()