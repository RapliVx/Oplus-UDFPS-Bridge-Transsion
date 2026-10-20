#![allow(non_snake_case)]

use oplus_udfps_bridge_transsion::android::hardware::biometrics::fingerprint::IFingerprint::BnFingerprint;
use oplus_udfps_bridge_transsion::service::FingerprintService;
use oplus_udfps_bridge_transsion::{log_error, log_info};
use oplus_udfps_bridge_transsion::SERVICE_NAME;

#[cfg(target_os = "android")]
extern "C" {
    fn __system_property_get(name: *const libc::c_char, value: *mut libc::c_char) -> libc::c_int;
    fn __system_property_set(name: *const libc::c_char, value: *const libc::c_char) -> libc::c_int;
}

#[cfg(not(target_os = "android"))]
unsafe fn __system_property_get(_name: *const libc::c_char, _value: *mut libc::c_char) -> libc::c_int {
    0
}

#[cfg(not(target_os = "android"))]
unsafe fn __system_property_set(_name: *const libc::c_char, _value: *const libc::c_char) -> libc::c_int {
    0
}

fn get_system_prop(key: &[u8], default: &str) -> String {
    let mut val = [0u8; 92];
    let len = unsafe {
        __system_property_get(key.as_ptr() as *const libc::c_char, val.as_mut_ptr() as *mut libc::c_char)
    };
    if len > 0 {
        String::from_utf8_lossy(&val[..len as usize]).into_owned()
    } else {
        default.to_string()
    }
}

fn set_system_prop(key: &[u8], value: &[u8]) {
    unsafe {
        __system_property_set(
            key.as_ptr() as *const libc::c_char,
            value.as_ptr() as *const libc::c_char,
        );
    }
}

fn main() -> anyhow::Result<()> {
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
    }

    // Ensure ColorOS AuthService recognises this sensor as a modern AIDL sensor
    set_system_prop(b"vendor.fingerprint.aidl.support\0", b"1\0");

    // Configure ColorOS optical icon properties for Under-Display Fingerprint (UDFPS).
    // Ensure iconsize is at least 500 (matching special.prop and the 540x2172 calibrated sensor).
    // NEVER set iconsize to 0 or iconnumber to "none", as ColorOS SystemUI uses them for
    // OnScreenFingerprintPressedIcon and aodFingerPrintIconSize; setting them to 0 collapses
    // the pressed icon window to 0x0 pixel and makes the AOD icon disappear on touch down.
    let current_iconsize = get_system_prop(b"persist.vendor.fingerprint.optical.iconsize\0", "");
    if current_iconsize.is_empty() || current_iconsize == "0" {
        set_system_prop(b"persist.vendor.fingerprint.optical.iconsize\0", b"500\0");
    }

    let current_iconnumber = get_system_prop(b"persist.vendor.fingerprint.optical.iconnumber\0", "");
    if current_iconnumber.is_empty() || current_iconnumber == "none" {
        set_system_prop(b"persist.vendor.fingerprint.optical.iconnumber\0", b"0\0");
    }

    let current_iconloc = get_system_prop(b"persist.vendor.fingerprint.optical.iconlocation\0", "");
    if current_iconloc.is_empty() {
        set_system_prop(b"persist.vendor.fingerprint.optical.iconlocation\0", b"228\0");
    }

    // Ensure ColorOS animations run at platform level 2 (frame-by-frame webp drawables; level 1 requires missing WaterRippleAnimation.coz)
    let anim_lvl = get_system_prop(b"persist.sys.oplus.anim_level\0", "");
    if anim_lvl.is_empty() || anim_lvl == "1" || anim_lvl == "-1" {
        set_system_prop(b"persist.sys.oplus.anim_level\0", b"2\0");
    }

    // Disable ColorOS biometrics screen-on blocking policy.
    // On Transsion hardware (Helio G99 / MT6789), blockScreenOnByBiometrics keeps the AMOLED panel
    // completely powered off / black during authentication, causing Silead to capture pure darkness and fail.
    // Setting this to false allows the display to wake immediately so HBM can illuminate the finger!
    set_system_prop(b"persist.sys.block.fingerprint\0", b"false\0");

    // Ensure gesture FOD is enabled in kernel driver
    oplus_udfps_bridge_transsion::input::ensure_gesture_fod_enabled();

    let brand = get_system_prop(b"ro.product.brand\0", "Transsion");
    let model = get_system_prop(b"ro.product.model\0", "Device");
    let platform = get_system_prop(b"ro.board.platform\0", "Universal");

    log_info!(
        "Starting android.hardware.biometrics.fingerprint-service for Transsion ({} {}, SoC: {})...",
        brand, model, platform
    );

    // Initialize Binder process state FIRST
    rsbinder::ProcessState::init_default().map_err(|error| {
        anyhow::anyhow!("failed to initialize Binder process state: {error}")
    })?;
    rsbinder::ProcessState::start_thread_pool();

    // Register AIDL fingerprint service IMMEDIATELY so ServiceManager & system_server never wait/deadlock
    let service = FingerprintService::new();
    let binder = BnFingerprint::new_binder(service);
    rsbinder::hub::add_service(SERVICE_NAME, binder.as_binder())?;

    log_info!(
        "Successfully registered {} with ServiceManager! Waiting for transactions...",
        SERVICE_NAME
    );

    // Asynchronously initialize FingerprintHal, touch listeners and HBM controller in background
    std::thread::spawn(|| {
        log_info!("Initializing FingerprintHal and background listeners...");
        match oplus_udfps_bridge_transsion::hal::FingerprintHal::get_instance() {
            Ok(_) => log_info!("FingerprintHal successfully initialized in background!"),
            Err(e) => log_error!("Failed to initialize FingerprintHal in background: {e}"),
        }
    });

    rsbinder::ProcessState::join_thread_pool()?;
    Ok(())
}
