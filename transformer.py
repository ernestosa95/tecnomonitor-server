from datetime import datetime
import json

def transformar_v2_a_v3(data_v2: dict) -> dict:
    """
    Convierte payload legacy (v2.x) al estándar V3.0
    Versión blindada contra NoneTypes y datos sucios.
    """
    # 1. ENVELOPE
    header = data_v2.get("header") or {} # Protección si header es None
    timestamp_str = header.get("timestamp")
    if not timestamp_str:
        timestamp_str = datetime.now().isoformat()

    envelope = {
        "schema_version": "3.0",
        "agent_version": header.get("agent_version", "2.x"),
        "hospital_id": header.get("hospital_id", "UNKNOWN"),
        "timestamp": timestamp_str
    }

    # 2. PHYSICAL LAYER
    phy_v2 = data_v2.get("physical_host") or {}
    env_v2 = data_v2.get("environment") or {}
    thermal_v2 = env_v2.get("thermal") or {}
    power_v2 = env_v2.get("power") or {}

    # Host Info
    host_info = {
        "hostname": "Unknown (Legacy)", 
        "type": "proxmox", 
        "model": phy_v2.get("model", "Unknown CPU"),
        "uptime_seconds": int(phy_v2.get("uptime_seconds") or 0)
    }

    # Telemetry Host
    ram_total = float(phy_v2.get("ram_total_gb") or 0)
    ram_used = float(phy_v2.get("ram_usage_gb") or 0)
    ram_pct = (ram_used / ram_total * 100) if ram_total > 0 else 0

    phy_telemetry = {
        "cpu": { "usage_percent": float(phy_v2.get("cpu_usage_percent") or 0) },
        "ram": {
            "total_gb": ram_total,
            "used_gb": ram_used,
            "usage_percent": round(ram_pct, 2)
        }
    }

    # Sensors
    temps = []
    # Protección: or [] asegura que iteramos una lista vacía si es None
    for t in (thermal_v2.get("cpu_temps") or []):
        if not isinstance(t, dict): continue
        temps.append({
            "name": t.get("sensor", "Unknown"),
            "value": float(t.get("temp_c") or 0),
            "unit": "C",
            "status": "OK"
        })
        
    if thermal_v2.get("ambient_temp_c"):
        temps.append({
            "name": "Ambient Temp",
            "value": float(thermal_v2.get("ambient_temp_c")),
            "unit": "C",
            "status": "OK"
        })

    fans = []
    for f in (thermal_v2.get("fans") or []):
        if not isinstance(f, dict): continue
        fans.append({
            "name": f.get("name", "Fan"),
            "value": float(f.get("speed_rpm") or 0),
            "unit": "RPM",
            "status": f.get("status", "OK")
        })

    supplies = []
    for p in (power_v2.get("power_supplies") or []):
        if not isinstance(p, dict): continue
        supplies.append({
            "name": p.get("name", "PSU"),
            "watts": float(p.get("output_watts") or 0),
            "status": p.get("status", "OK")
        })

    physical_layer = {
        "host_info": host_info,
        "telemetry": phy_telemetry,
        "sensors": {
            "status": env_v2.get("status", "OK"),
            "temperatures": temps,
            "fans": fans,
            "power": {
                "watts_current": float(power_v2.get("watts_consumed") or 0),
                "supplies": supplies
            }
        }
    }

    # 3. VIRTUAL LAYER
    vms_v2 = data_v2.get("vms") or {}
    virtual_layer = []

    for vm_id, vm_data in vms_v2.items():
        if not vm_data: continue # Saltar si la VM es None
        
        metrics = vm_data.get("metrics") or {}
        
        # Telemetria VM
        ram_vm = metrics.get("ram") or {}
        vm_telemetry = {
            "cpu": { "usage_percent": float(metrics.get("cpu_load_percent") or 0) },
            "uptime_seconds": int(metrics.get("uptime_seconds") or 0),
            "ram": {
                "total_gb": float(ram_vm.get("total_gb") or 0),
                "used_gb": float(ram_vm.get("used_gb") or 0),
                "usage_percent": float(ram_vm.get("percent") or 0)
            }
        }

        # Storage
        storage_list = []
        discos = metrics.get("discos") or {} # Protección si 'discos' es None
        for mount, disk in discos.items():
            if not disk: continue # Protección si el disco individual es None
            storage_list.append({
                "mount_point": mount,
                "total_gb": float(disk.get("total_gb") or 0),
                "free_gb": float(disk.get("libre_gb") or 0),
                "usage_percent": float(disk.get("percent_used") or 0),
                "performance": {
                    "latency_ms": float(disk.get("latency_ms") or 0),
                    "status": disk.get("latency_status", "OK")
                }
            })

        # Services
        services_list = []
        servicios = metrics.get("servicios") or {} # Protección si 'servicios' es None
        
        for svc_name, svc_data in servicios.items():
            # AQUI ESTABA EL ERROR: svc_data podía ser None
            if svc_data is None: 
                continue # Saltamos servicios nulos
                
            if isinstance(svc_data, str):
                services_list.append({
                    "name": svc_name,
                    "state": svc_data,
                    "vital_signs": None
                })
            else:
                # Asumimos que es dict
                services_list.append({
                    "name": svc_name,
                    "display_name": svc_name,
                    "state": svc_data.get("status", "Unknown"),
                    "vital_signs": {
                        "pid": int(svc_data.get("pid") or 0),
                        "health": svc_data.get("health", "OK"),
                        "cpu_percent": float(svc_data.get("cpu_percent") or 0),
                        "ram_mb": float(svc_data.get("ram_mb") or 0),
                        "threads": int(svc_data.get("threads") or 0),
                        "handles": int(svc_data.get("handles") or 0)
                    }
                })

        virtual_layer.append({
            "id": vm_id,
            "type": "vm",
            "state": vm_data.get("status", "Unknown"),
            "telemetry": vm_telemetry,
            "storage": storage_list,
            "application_layer": {
                "services": services_list
            }
        })

    return {
        "envelope": envelope,
        "physical_layer": physical_layer,
        "virtual_layer": virtual_layer
    }