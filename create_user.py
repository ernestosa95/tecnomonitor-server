"""
=============================================================================
 TecnoMonitor — Herramienta de Gestión de Usuarios
=============================================================================
 Uso:
   python create_admin.py              → menú interactivo
   python create_admin.py --listar     → muestra todos los usuarios activos
   python create_admin.py --desactivar usuario@tecnoimagen.com.ar

 Roles disponibles:
   Admin      → acceso total (crear, editar, eliminar hospitales, config)
   Ingenieria → acceso operativo (monitor, alertas, config, hospitales)
   Comercial  → acceso a KPIs y reportes PDF
   Visor      → solo lectura del monitor en tiempo real
=============================================================================
"""

import sys
import argparse
from database import SessionLocal, UserModel
from auth import get_password_hash

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
DOMINIO = "@tecnoimagen.com.ar"

ROLES_VALIDOS = {
    "1": "Admin",
    "2": "Ingenieria",
    "3": "Comercial",
    "4": "Visor",
}

DESCRIPCION_ROLES = {
    "Admin":      "Acceso total. Puede eliminar hospitales y gestionar usuarios.",
    "Ingenieria": "Acceso operativo. Monitor, alertas, config y gestión de hospitales.",
    "Comercial":  "Acceso a KPIs de uso y generación de reportes PDF.",
    "Visor":      "Solo lectura del monitor en tiempo real.",
}

PASSWORD_TEMPORAL = "Tecno2026."   # Contraseña inicial para todos los usuarios nuevos

# ---------------------------------------------------------------------------
# Helpers de presentación
# ---------------------------------------------------------------------------
def separador(caracter="─", ancho=60):
    print(caracter * ancho)

def titulo(texto):
    separador()
    print(f"  {texto}")
    separador()

def mostrar_roles():
    print("\n  Roles disponibles:")
    for num, rol in ROLES_VALIDOS.items():
        print(f"    [{num}] {rol:12s} — {DESCRIPCION_ROLES[rol]}")
    print()

# ---------------------------------------------------------------------------
# Operaciones de base de datos
# ---------------------------------------------------------------------------

def listar_usuarios(db, solo_activos=True):
    """Muestra en consola todos los usuarios registrados."""
    titulo("👥  USUARIOS REGISTRADOS")
    query = db.query(UserModel)
    if solo_activos:
        query = query.filter(UserModel.is_active == True)
    usuarios = query.order_by(UserModel.role, UserModel.full_name).all()

    if not usuarios:
        print("  (no hay usuarios registrados)")
        return

    fmt = "  {:<30} {:<14} {:<12} {}"
    print(fmt.format("EMAIL", "ROL", "ESTADO", "NOMBRE"))
    separador("·")
    for u in usuarios:
        estado = "✅ Activo" if u.is_active else "🚫 Inactivo"
        print(fmt.format(u.email, u.role, estado, u.full_name or "—"))
    separador()
    print(f"  Total: {len(usuarios)} usuario(s)\n")


def crear_usuario(db):
    """Flujo interactivo para crear un usuario nuevo."""
    titulo("➕  CREAR USUARIO NUEVO")
    mostrar_roles()

    # — Rol —
    opcion = input("  Seleccioná el rol [1-4]: ").strip()
    if opcion not in ROLES_VALIDOS:
        print("❌ Opción inválida. Operación cancelada.")
        return
    rol = ROLES_VALIDOS[opcion]

    # — Email —
    email_raw = input(f"  Email ({DOMINIO}): ").strip().lower()
    if not email_raw:
        print("❌ Email vacío. Operación cancelada.")
        return
    # Permitir que ingresen solo el prefijo
    if "@" not in email_raw:
        email_raw = email_raw + DOMINIO
    if not email_raw.endswith(DOMINIO):
        print(f"❌ El email debe pertenecer al dominio {DOMINIO}")
        return

    # — Nombre completo —
    nombre = input("  Nombre completo: ").strip()
    if not nombre:
        print("❌ Nombre vacío. Operación cancelada.")
        return

    # — Verificar duplicado —
    existente = db.query(UserModel).filter(UserModel.email == email_raw).first()
    if existente:
        if existente.is_active:
            print(f"\n⚠️  El usuario {email_raw} ya existe y está activo.")
        else:
            print(f"\n⚠️  El usuario {email_raw} existe pero está INACTIVO.")
            reactivar = input("  ¿Querés reactivarlo con el nuevo rol? [s/N]: ").strip().lower()
            if reactivar == "s":
                existente.is_active = True
                existente.role = rol
                existente.full_name = nombre
                existente.hashed_password = get_password_hash(PASSWORD_TEMPORAL)
                db.commit()
                print(f"\n✅ Usuario reactivado: {nombre} ({email_raw}) → Rol: {rol}")
                print(f"   Contraseña temporal restablecida: {PASSWORD_TEMPORAL}")
        return

    # — Confirmar —
    print(f"\n  Resumen:")
    print(f"    Email  : {email_raw}")
    print(f"    Nombre : {nombre}")
    print(f"    Rol    : {rol}  —  {DESCRIPCION_ROLES[rol]}")
    print(f"    Clave  : {PASSWORD_TEMPORAL} (deberá cambiarla al primer acceso)")
    confirmar = input("\n  ¿Confirmar creación? [s/N]: ").strip().lower()
    if confirmar != "s":
        print("  Operación cancelada.")
        return

    # — Crear —
    try:
        nuevo = UserModel(
            email=email_raw,
            hashed_password=get_password_hash(PASSWORD_TEMPORAL),
            full_name=nombre,
            role=rol,
            is_active=True,
        )
        db.add(nuevo)
        db.commit()
        print(f"\n✅ Usuario creado correctamente.")
        print(f"   {nombre} ({email_raw}) → Rol: {rol}")
        print(f"   Contraseña temporal: {PASSWORD_TEMPORAL}\n")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Error al crear el usuario: {e}")


def cambiar_rol(db):
    """Cambia el rol de un usuario existente."""
    titulo("✏️   CAMBIAR ROL DE USUARIO")
    listar_usuarios(db)

    email = input("  Email del usuario a modificar: ").strip().lower()
    if "@" not in email:
        email = email + DOMINIO

    usuario = db.query(UserModel).filter(UserModel.email == email).first()
    if not usuario:
        print(f"❌ No se encontró el usuario: {email}")
        return

    print(f"\n  Usuario : {usuario.full_name} ({usuario.email})")
    print(f"  Rol actual: {usuario.role}")
    mostrar_roles()

    opcion = input("  Nuevo rol [1-4]: ").strip()
    if opcion not in ROLES_VALIDOS:
        print("❌ Opción inválida.")
        return

    nuevo_rol = ROLES_VALIDOS[opcion]
    if nuevo_rol == usuario.role:
        print("  El usuario ya tiene ese rol. No se realizaron cambios.")
        return

    confirmar = input(f"\n  ¿Cambiar rol de '{usuario.role}' a '{nuevo_rol}'? [s/N]: ").strip().lower()
    if confirmar != "s":
        print("  Operación cancelada.")
        return

    try:
        usuario.role = nuevo_rol
        db.commit()
        print(f"\n✅ Rol actualizado: {usuario.full_name} → {nuevo_rol}\n")
    except Exception as e:
        db.rollback()
        print(f"❌ Error al actualizar: {e}")


def resetear_password(db):
    """Resetea la contraseña de un usuario a la temporal."""
    titulo("🔑  RESETEAR CONTRASEÑA")
    listar_usuarios(db)

    email = input("  Email del usuario: ").strip().lower()
    if "@" not in email:
        email = email + DOMINIO

    usuario = db.query(UserModel).filter(UserModel.email == email).first()
    if not usuario:
        print(f"❌ No se encontró el usuario: {email}")
        return

    print(f"\n  Usuario: {usuario.full_name} ({usuario.email})")
    confirmar = input(f"  ¿Resetear contraseña a '{PASSWORD_TEMPORAL}'? [s/N]: ").strip().lower()
    if confirmar != "s":
        print("  Operación cancelada.")
        return

    try:
        usuario.hashed_password = get_password_hash(PASSWORD_TEMPORAL)
        db.commit()
        print(f"\n✅ Contraseña reseteada.")
        print(f"   Contraseña temporal: {PASSWORD_TEMPORAL}")
        print(f"   El usuario deberá cambiarla desde el panel.\n")
    except Exception as e:
        db.rollback()
        print(f"❌ Error al resetear: {e}")


def desactivar_usuario(db, email_arg=None):
    """Desactiva (baja lógica) un usuario. No elimina el registro."""
    titulo("🚫  DESACTIVAR USUARIO")

    if email_arg:
        email = email_arg.strip().lower()
    else:
        listar_usuarios(db)
        email = input("  Email del usuario a desactivar: ").strip().lower()

    if "@" not in email:
        email = email + DOMINIO

    usuario = db.query(UserModel).filter(UserModel.email == email).first()
    if not usuario:
        print(f"❌ No se encontró el usuario: {email}")
        return

    if not usuario.is_active:
        print(f"⚠️  El usuario {email} ya está inactivo.")
        return

    print(f"\n  Usuario: {usuario.full_name} ({usuario.email}) — Rol: {usuario.role}")
    confirmar = input("  ¿Confirmar desactivación? El usuario no podrá iniciar sesión. [s/N]: ").strip().lower()
    if confirmar != "s":
        print("  Operación cancelada.")
        return

    try:
        usuario.is_active = False
        db.commit()
        print(f"\n✅ Usuario desactivado: {usuario.full_name} ({email})\n")
    except Exception as e:
        db.rollback()
        print(f"❌ Error al desactivar: {e}")


# ---------------------------------------------------------------------------
# Menú principal
# ---------------------------------------------------------------------------

MENU_OPCIONES = {
    "1": ("👥  Listar usuarios",        lambda db: listar_usuarios(db, solo_activos=False)),
    "2": ("➕  Crear usuario",           crear_usuario),
    "3": ("✏️   Cambiar rol",             cambiar_rol),
    "4": ("🔑  Resetear contraseña",    resetear_password),
    "5": ("🚫  Desactivar usuario",      desactivar_usuario),
    "0": ("🚪  Salir",                   None),
}

def menu_interactivo():
    print("\n" + "=" * 60)
    print("   TecnoMonitor — Gestión de Usuarios")
    print("=" * 60)

    db = SessionLocal()
    try:
        while True:
            print("\n  ¿Qué querés hacer?")
            for num, (descripcion, _) in MENU_OPCIONES.items():
                print(f"    [{num}] {descripcion}")
            print()

            opcion = input("  Opción: ").strip()

            if opcion == "0":
                print("\n  👋 Hasta luego.\n")
                break
            elif opcion in MENU_OPCIONES:
                _, funcion = MENU_OPCIONES[opcion]
                print()
                funcion(db)
            else:
                print("  ❌ Opción inválida. Ingresá un número del menú.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Entrada por línea de comandos
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="TecnoMonitor — Gestión de usuarios desde consola.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--listar",
        action="store_true",
        help="Listar todos los usuarios (activos e inactivos).",
    )
    parser.add_argument(
        "--desactivar",
        metavar="EMAIL",
        help="Desactivar un usuario por su email.",
    )

    args = parser.parse_args()

    if args.listar:
        db = SessionLocal()
        try:
            listar_usuarios(db, solo_activos=False)
        finally:
            db.close()
    elif args.desactivar:
        db = SessionLocal()
        try:
            desactivar_usuario(db, email_arg=args.desactivar)
        finally:
            db.close()
    else:
        # Sin argumentos → menú interactivo
        menu_interactivo()


if __name__ == "__main__":
    main()