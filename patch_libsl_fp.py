#!/usr/bin/env python3
import sys
import os
import struct

"""
Automated patch for Transsion Silead FOD HAL (libsl_fp_impl.so)
for ColorOS 16/17 (OPlus) and AOSP Custom ROM ports.

Fixes:
- Bypasses kernel Netlink uevent wait in `silfp_cust_wait_ui_ready`.
  Instead of waiting up to 3 seconds on Netlink socket for TRAN_FULL_HBM_EVENT=FULL_HBM_SET,
  it sleeps 30ms for panel HBM illumination to rise and immediately returns 0 (SUCCESS).
- Eliminates sensor capture lag and unlock timeouts.
"""

def parse_elf_sections(elf):
    e_shoff = struct.unpack('<Q', elf[40:48])[0]
    e_shentsize = struct.unpack('<H', elf[58:60])[0]
    e_shnum = struct.unpack('<H', elf[60:62])[0]
    e_shstrndx = struct.unpack('<H', elf[62:64])[0]

    shstr_hdr = e_shoff + e_shstrndx * e_shentsize
    shstr_offset = struct.unpack('<Q', elf[shstr_hdr+24:shstr_hdr+32])[0]

    sections = {}
    sections_list = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size, sh_link, sh_info = struct.unpack('<IIQQQQII', elf[off:off+48])
        name = elf[shstr_offset + sh_name:].split(b'\0')[0].decode('latin1')
        sec_info = {
            'index': i,
            'name': name,
            'type': sh_type,
            'addr': sh_addr,
            'offset': sh_offset,
            'size': sh_size,
            'link': sh_link
        }
        sections[name] = sec_info
        sections_list.append(sec_info)
    return sections, sections_list

def find_symbol(elf, sections, symbol_name):
    dynsym = sections.get('.dynsym')
    if not dynsym:
        return None, None
    dynstr = sections.get('.dynstr')
    dynstr_off = dynstr['offset']

    for off in range(dynsym['offset'], dynsym['offset'] + dynsym['size'], 24):
        st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack('<IBBHQQ', elf[off:off+24])
        str_start = dynstr_off + st_name
        str_end = elf.find(b'\0', str_start)
        sym_name = elf[str_start:str_end].decode('latin1')
        if sym_name == symbol_name:
            if st_shndx < len(sections):
                # Look up section by index
                for sec in sections.values():
                    if sec['index'] == st_shndx:
                        file_offset = sec['offset'] + (st_value - sec['addr'])
                        return st_value, file_offset
    return None, None

def find_usleep_plt(elf, sections):
    rela = sections.get('.rela.plt')
    dynsym = sections.get('.dynsym')
    dynstr = sections.get('.dynstr')
    plt = sections.get('.plt')

    if not rela or not dynsym or not dynstr or not plt:
        return None

    dynstr_off = dynstr['offset']
    for i, off in enumerate(range(rela['offset'], rela['offset'] + rela['size'], 24)):
        r_offset, r_info, r_addend = struct.unpack('<QQq', elf[off:off+24])
        sym_idx = r_info >> 32
        sym_off = dynsym['offset'] + sym_idx * 24
        st_name = struct.unpack('<I', elf[sym_off:sym_off+4])[0]
        str_start = dynstr_off + st_name
        str_end = elf.find(b'\0', str_start)
        sym_name = elf[str_start:str_end].decode('latin1')
        if sym_name == 'usleep':
            # Entry 0 is PLT header (32 bytes), then 16 bytes per entry
            return plt['addr'] + 32 + i * 16
    return None

def patch_libsl_fp(filepath):
    if not os.path.isfile(filepath):
        print(f"Error: file not found: {filepath}")
        sys.exit(1)

    with open(filepath, 'rb') as f:
        elf = bytearray(f.read())

    sections, sections_list = parse_elf_sections(elf)

    vma, file_offset = find_symbol(elf, sections, 'silfp_cust_wait_ui_ready')
    if not vma or not file_offset:
        print("Warning: Symbol silfp_cust_wait_ui_ready not found in .dynsym, searching fallback pattern...")
        sig = bytes.fromhex('ff 83 01 d1 f5 1b 00 f9 f4 4f 04 a9 fd 7b 05 a9 fd 43 01 91 48 d0 3b d5')
        # Silead wait_ui_ready is typically at the highest offset among matches
        matches = []
        pos = 0
        while True:
            idx = elf.find(sig, pos)
            if idx == -1: break
            matches.append(idx)
            pos = idx + 1
        if matches:
            file_offset = matches[-1]
            text = sections.get('.text')
            vma = text['addr'] + (file_offset - text['offset'])
            print(f"Fallback matched function at file offset 0x{file_offset:x} (VMA 0x{vma:x})")
        else:
            print("Error: Could not locate silfp_cust_wait_ui_ready")
            sys.exit(1)

    plt_usleep = find_usleep_plt(elf, sections)
    if not plt_usleep:
        print("Error: Could not locate usleep in PLT")
        sys.exit(1)

    print(f"Target: silfp_cust_wait_ui_ready at VMA 0x{vma:x} (file offset 0x{file_offset:x})")
    print(f"usleep@plt: 0x{plt_usleep:x}")

    # Assemble ARM64 patch:
    # 0x00: stp x29, x30, [sp, #-16]!
    # 0x04: mov w0, #5000 (5ms sleep for optical HBM stabilization aligned with ColorOS animation)
    # 0x08: bl <usleep@plt>
    # 0x0C: mov w0, #0 (SUCCESS)
    # 0x10: ldp x29, x30, [sp], #16
    # 0x14: ret
    bl_addr = vma + 8
    diff = (plt_usleep - bl_addr) // 4
    bl_insn = 0x94000000 | (diff & 0x03ffffff)

    patch_bytes = struct.pack('<6I',
        0xa9bf7bfd,
        0x52827100,
        bl_insn,
        0x52800000,
        0xa8c17bfd,
        0xd65f03c0
    )

    elf[file_offset : file_offset + len(patch_bytes)] = patch_bytes

    # NOP out Silead vendor HAL HBM manipulation so fodbridge has exclusive control
    # 2. Patch silfp_cust_set_hbm_mode -> return 0
    vma_set_hbm, off_set_hbm = find_symbol(elf, sections, 'silfp_cust_set_hbm_mode')
    if off_set_hbm:
        print(f"Patching silfp_cust_set_hbm_mode at offset 0x{off_set_hbm:x} (return 0)")
        elf[off_set_hbm : off_set_hbm + 8] = struct.pack('<2I', 0x52800000, 0xd65f03c0)

    # 3. Patch silfp_cust_restore_hbm -> return 0
    vma_rst_hbm, off_rst_hbm = find_symbol(elf, sections, 'silfp_cust_restore_hbm')
    if off_rst_hbm:
        print(f"Patching silfp_cust_restore_hbm at offset 0x{off_rst_hbm:x} (return 0)")
        elf[off_rst_hbm : off_rst_hbm + 8] = struct.pack('<2I', 0x52800000, 0xd65f03c0)

    # 4. Patch silfp_dev_sync_finger_down to not check m_nl_inited (replace b.ne with nop)
    vma_sync_down, off_sync_down = find_symbol(elf, sections, 'silfp_dev_sync_finger_down')
    if off_sync_down:
        # At offset +12: 54 00 01 21 (b.ne +0x30)
        if elf[off_sync_down + 12 : off_sync_down + 16] == bytes.fromhex('21010054'):
            print(f"Patching silfp_dev_sync_finger_down at offset 0x{off_sync_down + 12:x} with NOP")
            elf[off_sync_down + 12 : off_sync_down + 16] = bytes.fromhex('1f2003d5')

    # 5. Patch silfp_dev_sync_finger_up to not check m_nl_inited (replace b.ne with nop)
    vma_sync_up, off_sync_up = find_symbol(elf, sections, 'silfp_dev_sync_finger_up')
    if off_sync_up:
        # At offset +12: 54 00 01 21 (b.ne +0x30)
        if elf[off_sync_up + 12 : off_sync_up + 16] == bytes.fromhex('21010054'):
            print(f"Patching silfp_dev_sync_finger_up at offset 0x{off_sync_up + 12:x} with NOP")
            elf[off_sync_up + 12 : off_sync_up + 16] = bytes.fromhex('1f2003d5')

    # 6. Patch silfp_impl_verify_enroll_challenge -> return 0 (bypass Keymaster HMAC / token check on DSU)
    vma_verify, off_verify = find_symbol(elf, sections, 'silfp_impl_verify_enroll_challenge')
    if off_verify:
        print(f"Patching silfp_impl_verify_enroll_challenge at offset 0x{off_verify:x} (return 0)")
        elf[off_verify : off_verify + 8] = struct.pack('<2I', 0x52800000, 0xd65f03c0)

    # 7. Patch silfp_dev_wait_finger_status -> force m_finger_status_mode = 1 (bypass ioctl 0x8000730b, use msg pipe)
    vma_wait, off_wait = find_symbol(elf, sections, 'silfp_dev_wait_finger_status')
    if off_wait:
        print(f"Patching silfp_dev_wait_finger_status at offset 0x{off_wait:x} to bypass ioctl blocking")
        for off in [0x18, 0x5c, 0xf0]:
            orig = struct.unpack('<I', elf[off_wait + off : off_wait + off + 4])[0]
            if orig == 0xb945c708:
                print(f"  Patching ldr w8, [x24, #0x5c4] at +0x{off:x} -> mov w8, #1")
                elf[off_wait + off : off_wait + off + 4] = struct.pack('<I', 0x52800028)

    # 8. Patch silfp_impl_wait_finger_up_with_cancel -> return 0 (prevent hanging on finger up in optical loop)
    vma_up_cancel, off_up_cancel = find_symbol(elf, sections, 'silfp_impl_wait_finger_up_with_cancel')
    if off_up_cancel:
        print(f"Patching silfp_impl_wait_finger_up_with_cancel at offset 0x{off_up_cancel:x} (return 0)")
        elf[off_up_cancel : off_up_cancel + 8] = struct.pack('<2I', 0x52800000, 0xd65f03c0)

    # 9. Patch silfp_impl_wait_finger_up_with_cancel_no_action -> return 0
    vma_up_no_act, off_up_no_act = find_symbol(elf, sections, 'silfp_impl_wait_finger_up_with_cancel_no_action')
    if off_up_no_act:
        print(f"Patching silfp_impl_wait_finger_up_with_cancel_no_action at offset 0x{off_up_no_act:x} (return 0)")
        elf[off_up_no_act : off_up_no_act + 8] = struct.pack('<2I', 0x52800000, 0xd65f03c0)

    outpath = filepath if filepath.endswith("_patched.so") else filepath + ".patched"
    with open(outpath, 'wb') as f:
        f.write(elf)

    print(f"Successfully generated patched library: {outpath}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 patch_libsl_fp.py <path_to_libsl_fp_impl.so>")
        sys.exit(1)
    patch_libsl_fp(sys.argv[1])
