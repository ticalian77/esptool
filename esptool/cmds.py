# SPDX-FileCopyrightText: 2014-2025 Fredrik Ahlberg, Angus Gratton,
# Espressif Systems (Shanghai) CO LTD, other contributors as noted.
#
# SPDX-License-Identifier: GPL-2.0-or-later

import hashlib
import io
import os
import struct
import sys
import time
import zlib
import itertools

from intelhex import IntelHex
from serial import SerialException
from typing import BinaryIO

from .bin_image import ELFFile, LoadFirmwareImage
from .bin_image import (
    ESP8266ROMFirmwareImage,
    ESP8266V2FirmwareImage,
    ESP8266V3FirmwareImage,
)
from .loader import (
    DEFAULT_CONNECT_ATTEMPTS,
    DEFAULT_TIMEOUT,
    ERASE_WRITE_TIMEOUT_PER_MB,
    ESPLoader,
    timeout_per_mb,
)
from .logger import log

from .targets import CHIP_DEFS, CHIP_LIST, ROM_LIST
from .uf2_writer import UF2Writer
from .util import (
    FatalError,
    NotImplementedInROMError,
    NotSupportedError,
    UnsupportedCommandError,
)
from .util import (
    div_roundup,
    flash_size_bytes,
    get_file_size,
    hexify,
    pad_to,
    sanitize_string,
)

DETECTED_FLASH_SIZES = {
    0x12: "256KB",
    0x13: "512KB",
    0x14: "1MB",
    0x15: "2MB",
    0x16: "4MB",
    0x17: "8MB",
    0x18: "16MB",
    0x19: "32MB",
    0x1A: "64MB",
    0x1B: "128MB",
    0x1C: "256MB",
    0x20: "64MB",
    0x21: "128MB",
    0x22: "256MB",
    0x32: "256KB",
    0x33: "512KB",
    0x34: "1MB",
    0x35: "2MB",
    0x36: "4MB",
    0x37: "8MB",
    0x38: "16MB",
    0x39: "32MB",
    0x3A: "64MB",
}

FLASH_MODES = {
    "qio": 0,
    "qout": 1,
    "dio": 2,
    "dout": 3,
}


def detect_chip(
    port: str = ESPLoader.DEFAULT_PORT,
    baud: int = ESPLoader.ESP_ROM_BAUD,
    connect_mode: str = "default_reset",
    trace_enabled: bool = False,
    connect_attempts: int = DEFAULT_CONNECT_ATTEMPTS,
) -> ESPLoader:
    """
    Detect the type of ESP device connected via serial,
    connect to it, and return an active ESPLoader object.

    Args:
        port: The serial port to use for communication.
        baud: The baud rate for serial communication.
        connect_mode: The chip reset method to perform when connecting to the ESP device
            (``"default_reset"``, ``"usb_reset"``,
            ``"no_reset"``, ``"no_reset_no_sync"``)
        trace_enabled: Enables or disables tracing for debugging purposes.
        connect_attempts: Number of connection attempts before failing.

    Returns:
        An initialized instance of the detected chip class ready for use.
    """
    inst = None
    detect_port = ESPLoader(port, baud, trace_enabled=trace_enabled)
    if detect_port.serial_port.startswith("rfc2217:"):
        detect_port.USES_RFC2217 = True
    detect_port.connect(connect_mode, connect_attempts, detecting=True)

    def check_if_stub(instance: ESPLoader) -> ESPLoader:
        log.print(f" {instance.CHIP_NAME}")
        if detect_port.sync_stub_detected and instance.STUB_CLASS is not None:
            instance = instance.STUB_CLASS(instance)
            instance.sync_stub_detected = True
        return instance

    """
    First, get_security_info command is sent to detect the ID of the chip
    (supported only by ESP32-C3 and later, works even in the Secure Download Mode).
    If this fails, we reconnect and fall-back to reading the magic number.
    It's mapped at a specific ROM address and has a different value on each chip model.
    This way we use one memory read and compare it to the magic number for each chip.
    """
    try:
        log.print("Detecting chip type...", end="")
        chip_id = detect_port.get_chip_id()
        for cls in ROM_LIST:
            # cmd not supported on ESP8266 and ESP32 + ESP32-S2 doesn't return chip_id
            if cls.USES_MAGIC_VALUE:
                continue
            if chip_id == cls.IMAGE_CHIP_ID:
                inst = cls(detect_port._port, baud, trace_enabled=trace_enabled)
                try:
                    inst.read_reg(
                        ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR
                    )  # Dummy read to check Secure Download mode
                except UnsupportedCommandError:
                    inst.secure_download_mode = True
                inst = check_if_stub(inst)
                inst._post_connect()
                break
        else:
            err_msg = f"Unexpected chip ID value {chip_id}."
    except (UnsupportedCommandError, struct.error, FatalError) as e:
        # UnsupportedCommandError: ESP8266/ESP32 ROM
        # struct.error: ESP32-S2
        # FatalError: ESP8266/ESP32 STUB
        log.print(" Unsupported detection protocol, switching and trying again...")
        try:
            # ESP32/ESP8266 are reset after an unsupported command, need to reconnect
            # (not needed on ESP32-S2)
            if not isinstance(e, struct.error):
                detect_port.connect(
                    connect_mode, connect_attempts, detecting=True, warnings=False
                )
            log.print("Detecting chip type...", end="", flush=True)
            chip_magic_value = detect_port.read_reg(
                ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR
            )

            for cls in ROM_LIST:
                if not cls.USES_MAGIC_VALUE:
                    continue
                if chip_magic_value == cls.MAGIC_VALUE:
                    inst = cls(detect_port._port, baud, trace_enabled=trace_enabled)
                    inst = check_if_stub(inst)
                    inst._post_connect()
                    break
            else:
                err_msg = f"Unexpected chip magic value {chip_magic_value:#010x}."
        except UnsupportedCommandError:
            raise FatalError(
                "Unsupported Command Error received. "
                "Probably this means Secure Download Mode is enabled, "
                "autodetection will not work. Need to manually specify the chip."
            )
    if inst is not None:
        return inst

    raise FatalError(
        f"{err_msg} Failed to autodetect chip type."
        "\nProbably it is unsupported by this version of esptool."
    )


# Commands that require an ESP object
#####################################


def load_ram(esp: ESPLoader, filename: str) -> None:
    """
    Load a firmware image into RAM and execute it on the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
        filename: Path to the firmware image file.
    """
    image = LoadFirmwareImage(esp.CHIP_NAME, filename)

    log.print("RAM boot...")
    for seg in image.segments:
        size = len(seg.data)
        log.print(f"Downloading {size} bytes at {seg.addr:08x}...", end=" ", flush=True)
        esp.mem_begin(
            size, div_roundup(size, esp.ESP_RAM_BLOCK), esp.ESP_RAM_BLOCK, seg.addr
        )

        seq = 0
        while len(seg.data) > 0:
            esp.mem_block(seg.data[0 : esp.ESP_RAM_BLOCK], seq)
            seg.data = seg.data[esp.ESP_RAM_BLOCK :]
            seq += 1
        log.print("done!")

    log.print(f"All segments done, executing at {image.entrypoint:08x}")
    esp.mem_finish(image.entrypoint)


def read_mem(esp: ESPLoader, address: int) -> None:
    """
    Read and display a 32-bit value from a memory address on the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
        address: Memory address to read from (32-bit aligned).
    """
    log.print(f"{address:#010x} = {esp.read_reg(address):#010x}")


def write_mem(esp: ESPLoader, address: int, value: int, mask: int = 0xFFFFFFFF) -> None:
    """
    Write a 32-bit value to a memory address on the ESP device with optional bitmask.

    Args:
        esp: Initiated esp object connected to a real device.
        address: Memory address to write to (32-bit aligned).
        value: 32-bit value to write.
        mask: Bitmask specifying which bits to modify (default: all bits).
    """
    esp.write_reg(address, value, mask, 0)
    log.print(f"Wrote {value:#010x}, mask {mask:#010x} to {address:#010x}")


def dump_mem(
    esp: ESPLoader, address: int, size: int, output: str | None = None
) -> bytes | None:
    """
    Dump a block of memory from the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
        address: Starting memory address to dump from.
        size: Number of bytes to dump.
        output: Path to output file for binary data. If None, returns the data.

    Returns:
        Memory dump as bytes if filename is None;
        otherwise, returns None after writing to file.
    """
    log.print(
        f"Dumping {size} bytes from {address:#010x}"
        + (f" to file '{output}'..." if output else "...")
    )
    data = bytearray()
    log.set_progress(0)

    # Read the memory in 4-byte chunks.
    for i in range(size // 4):
        d = esp.read_reg(address + (i * 4))
        data.extend(struct.pack("<I", d))
        # Update progress every 1024 bytes.
        if len(data) % 1024 == 0:
            percent = len(data) * 100 // size
            log.set_progress(percent)
            log.print_overwrite(f"{len(data)} bytes read... ({percent}%)")

    log.print_overwrite(f"Successfully read {len(data)} bytes.", last_line=True)

    if output:
        with open(output, "wb") as f:
            f.write(data)
        log.print(f"Memory dump to '{output}' completed.")
        return None
    else:
        log.print("Memory dump completed.")
        return bytes(data)


def detect_flash_size(esp: ESPLoader) -> str | None:
    """
    Detect the flash size of the connected ESP device.

    Args:
        esp: Initiated esp object connected to a real device.

    Returns:
        Detected flash size in bytes, or None if unrecognized.
    """
    if esp.secure_download_mode:
        raise FatalError(
            "Detecting flash size is not supported in secure download mode. "
            "Need to manually specify flash size."
        )
    flash_id = esp.flash_id()
    size_id = flash_id >> 16
    flash_size = DETECTED_FLASH_SIZES.get(size_id)
    return flash_size


def _update_image_flash_params(esp, address, flash_freq, flash_mode, flash_size, image):
    """
    Update the flash mode, size, and freq parameters in a bootloader image,
    if applicable.

    Args:
        esp (ESPLoader): ESPLoader object that provides device-specific attributes
            (e.g., BOOTLOADER_FLASH_OFFSET, ESP_IMAGE_MAGIC, CHIP_NAME) and methods for
            image verification and parameter parsing.
        address (int): The flash memory address where the image is to be written.
        flash_freq (str, optional): Flash frequency setting
            (``"keep"`` to retain current).
        flash_mode (str, optional): Flash mode setting
            (``"keep"`` to retain current).
        flash_size (str, optional): Flash size setting
            (``"keep"`` to retain current).
        image (bytes): The image data that may contain an executable bootloader image.

    Returns:
        bytes: The modified image data with updated flash parameters
            (and recalculated SHA256 digest, if applicable),
            or the original image if no modifications were performed.
    """
    if len(image) < 8:
        return image  # not long enough to be a bootloader image
    if address != esp.BOOTLOADER_FLASH_OFFSET:
        return image  # not flashing bootloader offset, so don't modify this
    if (flash_mode, flash_freq, flash_size) == ("keep",) * 3:
        return image  # all settings are 'keep', not modifying anything

    # unpack the (potential) image header
    magic, _, img_flash_mode, img_flash_size_freq = struct.unpack("BBBB", image[:4])

    # easy check if this is an image: does it start with a magic byte?
    if magic != esp.ESP_IMAGE_MAGIC:
        log.warning(
            f"Image file at {address:#x} doesn't look like an image file, "
            "so not changing any flash settings."
        )
        return image

    # make sure this really is an image, and not just data that
    # starts with esp.ESP_IMAGE_MAGIC (mostly a problem for encrypted
    # images that happen to start with a magic byte
    try:
        test_image = esp.BOOTLOADER_IMAGE(io.BytesIO(image))
        test_image.verify()
    except Exception:
        log.warning(
            f"Image file at {address:#x} is not a valid {esp.CHIP_NAME} image,"
            " so not changing any flash settings."
        )
        return image

    # After the 8-byte header comes the extended header for chips others than ESP8266.
    # The 15th byte of the extended header indicates if the image is protected by SHA256
    # checksum. In that case we recalculate the SHA digest after modifying the header.
    sha_appended = esp.CHIP_NAME != "esp8266" and image[8 + 15] == 1

    if flash_mode != "keep":
        img_flash_mode = FLASH_MODES[flash_mode]

    img_flash_freq = img_flash_size_freq & 0x0F
    if flash_freq != "keep":
        img_flash_freq = esp.parse_flash_freq_arg(flash_freq)

    img_flash_size = img_flash_size_freq & 0xF0
    if flash_size != "keep":
        img_flash_size = esp.parse_flash_size_arg(flash_size)

    flash_params = struct.pack(b"BB", img_flash_mode, img_flash_size + img_flash_freq)
    if flash_params != image[2:4]:
        log.print(f"Flash params set to {struct.unpack('>H', flash_params)[0]:#06x}")
        image = image[0:2] + flash_params + image[4:]

    # recalculate the SHA digest if it was appended
    if sha_appended:
        # Since the changes are only made for images located in the bootloader offset,
        # we can assume that the image is always a bootloader image.
        # For merged binaries, we check the bootloader SHA when parameters are changed.
        image_object = esp.BOOTLOADER_IMAGE(io.BytesIO(image))
        # get the image header, extended header (if present) and data
        image_data_before_sha = image[: image_object.data_length]
        # get the image data after the SHA digest (primary for merged binaries)
        image_data_after_sha = image[
            (image_object.data_length + image_object.SHA256_DIGEST_LEN) :
        ]

        sha_digest_calculated = hashlib.sha256(image_data_before_sha).digest()
        image = bytes(
            itertools.chain(
                image_data_before_sha, sha_digest_calculated, image_data_after_sha
            )
        )

        # get SHA digest newly stored in the image and compare it to the calculated one
        image_stored_sha = image[
            image_object.data_length : image_object.data_length
            + image_object.SHA256_DIGEST_LEN
        ]

        if hexify(sha_digest_calculated) == hexify(image_stored_sha):
            log.print("SHA digest in image updated")
        else:
            log.warning(
                "SHA recalculation for binary failed!\n"
                f"\tExpected calculated SHA: {hexify(sha_digest_calculated)}\n"
                f"\tSHA stored in binary:    {hexify(image_stored_sha)}"
            )

    return image


def write_flash(
    esp: ESPLoader,
    addr_filename: list[tuple[int, BinaryIO]],
    flash_freq: str = "keep",
    flash_mode: str = "keep",
    flash_size: str = "keep",
    **kwargs,
) -> None:
    """
    Write firmware or data to the SPI flash memory of an ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
        addr_filename: List of (address, file) tuples specifying where
            to write each file in flash memory.
        flash_freq: Flash frequency to set in the bootloader image header
            (``"keep"`` to retain current).
        flash_mode: Flash mode to set in the bootloader image header
            (``"keep"`` to retain current).
        flash_size: Flash size to set in the bootloader image header
            (``"keep"`` to retain current).

    Keyword Args:
        erase_all (bool): Erase the entire flash before writing.
        encrypt (bool): Encrypt all files during flashing.
        encrypt_files (list[tuple[int, BinaryIO]] | None): List of (address, file)
            tuples for files to encrypt individually.
        compress (bool): Compress data before flashing.
        no_compress (bool): Don't compress data before flashing.
        force (bool): Ignore safety checks (e.g., overwriting bootloader, flash size).
        ignore_flash_enc_efuse (bool): Ignore flash encryption eFuse settings.
        no_progress (bool): Disable progress updates.
    """

    # Set default values of optional arguments
    erase_all: bool = kwargs.get("erase_all", False)
    encrypt: bool = kwargs.get("encrypt", False)
    encrypt_files: list[tuple[int, BinaryIO]] | None = kwargs.get("encrypt_files", None)
    compress: bool = kwargs.get("compress", False)
    no_compress: bool = kwargs.get("no_compress", False)
    force: bool = kwargs.get("force", False)
    ignore_flash_enc_efuse: bool = kwargs.get("ignore_flash_enc_efuse", False)
    no_progress: bool = kwargs.get("no_progress", False)

    # set compress based on default behaviour:
    # -> if either "compress" or "no_compress" is set, honour that
    # -> otherwise, set "compress" unless the stub flasher is disabled
    if not compress and not no_compress:
        compress = esp.IS_STUB

    if not force and esp.CHIP_NAME != "ESP8266" and not esp.secure_download_mode:
        # Check if secure boot is active
        if esp.get_secure_boot_enabled():
            for address, _ in addr_filename:
                if address < 0x8000:
                    raise FatalError(
                        "Secure Boot detected, writing to flash regions < 0x8000 "
                        "is disabled to protect the bootloader. "
                        "Use the force argument to override, "
                        "please use with caution, otherwise it may brick your device!"
                    )
        # Check if chip_id and min_rev in image are valid for the target in use
        for _, argfile in addr_filename:
            try:
                image = LoadFirmwareImage(esp.CHIP_NAME, argfile)
            except (FatalError, struct.error, RuntimeError):
                continue
            finally:
                argfile.seek(0)  # LoadFirmwareImage changes the file handle position
            if image.chip_id != esp.IMAGE_CHIP_ID:
                raise FatalError(
                    f"{argfile.name} is not an {esp.CHIP_NAME} image. "
                    "Use the force argument to flash anyway."
                )

            # this logic below decides which min_rev to use, min_rev or min/max_rev_full
            if image.max_rev_full == 0:  # image does not have max/min_rev_full fields
                use_rev_full_fields = False
            elif image.max_rev_full == 65535:  # image has default value of max_rev_full
                use_rev_full_fields = True
                if (
                    image.min_rev_full == 0 and image.min_rev != 0
                ):  # min_rev_full is not set, min_rev is used
                    use_rev_full_fields = False
            else:  # max_rev_full set to a version
                use_rev_full_fields = True

            if use_rev_full_fields:
                rev = esp.get_chip_revision()
                if rev < image.min_rev_full or rev > image.max_rev_full:
                    error_str = f"{argfile.name} requires chip revision in range "
                    error_str += (
                        f"[v{image.min_rev_full // 100}.{image.min_rev_full % 100} - "
                    )
                    if image.max_rev_full == 65535:
                        error_str += "max rev not set] "
                    else:
                        error_str += (
                            f"v{image.max_rev_full // 100}.{image.max_rev_full % 100}] "
                        )
                    error_str += f"(this chip is revision v{rev // 100}.{rev % 100})"
                    raise FatalError(
                        f"{error_str}. Use the force argument to flash anyway."
                    )
            else:
                # In IDF, image.min_rev is set based on Kconfig option.
                # For C3 chip, image.min_rev is the Minor revision
                # while for the rest chips it is the Major revision.
                if esp.CHIP_NAME == "ESP32-C3":
                    rev = esp.get_minor_chip_version()
                else:
                    rev = esp.get_major_chip_version()
                if rev < image.min_rev:
                    raise FatalError(
                        f"{argfile.name} requires chip revision "
                        f"{image.min_rev} or higher (this chip is revision {rev}). "
                        "Use the force argument to flash anyway."
                    )

    # In case we have encrypted files to write,
    # we first do few sanity checks before actual flash
    if encrypt or encrypt_files is not None:
        do_write = True

        if not esp.secure_download_mode:
            if esp.get_encrypted_download_disabled():
                raise FatalError(
                    "This chip has encrypt functionality "
                    "in UART download mode disabled. "
                    "This is the Flash Encryption configuration for Production mode "
                    "instead of Development mode."
                )

            crypt_cfg_efuse = esp.get_flash_crypt_config()

            if crypt_cfg_efuse is not None and crypt_cfg_efuse != 0xF:
                log.print(f"Unexpected FLASH_CRYPT_CONFIG value: {crypt_cfg_efuse:#x}")
                do_write = False

            enc_key_valid = esp.is_flash_encryption_key_valid()

            if not enc_key_valid:
                log.print("Flash encryption key is not programmed")
                do_write = False

        # Determine which files list contain the ones to encrypt
        files_to_encrypt = addr_filename if encrypt else encrypt_files

        if files_to_encrypt is not None:
            for address, argfile in files_to_encrypt:
                if address % esp.FLASH_ENCRYPTED_WRITE_ALIGN:
                    log.print(
                        f"File {argfile.name} address {address:#x} is not "
                        f"{esp.FLASH_ENCRYPTED_WRITE_ALIGN} byte aligned, "
                        "can't flash encrypted"
                    )

                    do_write = False

        if not do_write and not ignore_flash_enc_efuse:
            raise FatalError(
                "Can't perform encrypted flash write, "
                "consult Flash Encryption documentation for more information"
            )
    else:
        if not force and esp.CHIP_NAME != "ESP8266":
            # ESP32 does not support `get_security_info()` and `secure_download_mode`
            if (
                esp.CHIP_NAME != "ESP32"
                and esp.secure_download_mode
                and bin(esp.get_security_info()["flash_crypt_cnt"]).count("1") & 1 != 0
            ):
                raise FatalError(
                    "WARNING: Detected flash encryption and "
                    "secure download mode enabled.\n"
                    "Flashing plaintext binary may brick your device! "
                    "Use the force argument to override the warning."
                )

            if (
                not esp.secure_download_mode
                and esp.get_encrypted_download_disabled()
                and esp.get_flash_encryption_enabled()
            ):
                raise FatalError(
                    "WARNING: Detected flash encryption enabled and "
                    "download manual encrypt disabled.\n"
                    "Flashing plaintext binary may brick your device! "
                    "Use the force argument to override the warning."
                )

    flash_size = _set_flash_parameters(esp, flash_size)  # Set flash size parameters

    set_flash_size = (
        flash_size_bytes(flash_size) if flash_size not in ["detect", "keep"] else None
    )
    if esp.secure_download_mode:
        flash_end = set_flash_size
    else:  # Check against real flash chip size if not in SDM
        flash_end_str = detect_flash_size(esp)
        flash_end = flash_size_bytes(flash_end_str)
        if set_flash_size and flash_end and set_flash_size > flash_end:
            log.warning(
                f"Set flash_size {flash_size} "
                f"is larger than the available flash size of {flash_end_str}."
            )

    # Verify file sizes fit in the set flash_size, or real flash size if smaller
    flash_end = min(set_flash_size, flash_end) if set_flash_size else flash_end
    if flash_end is not None:
        for address, argfile in addr_filename:
            argfile.seek(0, os.SEEK_END)
            if address + argfile.tell() > flash_end:
                raise FatalError(
                    f"File {argfile.name} (length {argfile.tell()}) at offset "
                    f"{address} will not fit in {flash_end} bytes of flash. "
                    "Change the flash_size argument, or flashing address."
                )
            argfile.seek(0)

    if erase_all:
        erase_flash(esp)
    else:
        for address, argfile in addr_filename:
            argfile.seek(0, os.SEEK_END)
            write_end = address + argfile.tell()
            argfile.seek(0)
            bytes_over = address % esp.FLASH_SECTOR_SIZE
            if bytes_over != 0:
                log.note(
                    f"Flash address {address:#010x} is not aligned "
                    f"to a {esp.FLASH_SECTOR_SIZE:#x} byte flash sector. "
                    f"{bytes_over:#x} bytes before this address will be erased."
                )
            # Print the address range of to-be-erased flash memory region
            log.print(
                "Flash will be erased from {:#010x} to {:#010x}...".format(
                    address - bytes_over,
                    div_roundup(write_end, esp.FLASH_SECTOR_SIZE)
                    * esp.FLASH_SECTOR_SIZE
                    - 1,
                )
            )

    """
    Create a list describing all the files we have to flash.
    Each entry holds an "encrypt" flag marking whether the file needs encryption or not.
    This list needs to be sorted.

    First, append to each entry of our addr_filename list the flag "encrypt"
    E.g., if addr_filename is [(0x1000, "partition.bin"), (0x8000, "bootloader")],
    all_files will be [
        (0x1000, "partition.bin", encrypt),
        (0x8000, "bootloader", encrypt)
        ],
    where, of course, encrypt is either True or False
    """
    all_files = [(offs, filename, encrypt) for (offs, filename) in addr_filename]

    """
    Now do the same with encrypt_files list, if defined.
    In this case, the flag is True
    """
    if encrypt_files is not None:
        encrypted_files_flag = [
            (offs, filename, True) for (offs, filename) in encrypt_files
        ]

        # Concatenate both lists and sort them.
        # As both list are already sorted, we could simply do a merge instead,
        # but for the sake of simplicity and because the lists are very small,
        # let's use sorted.
        all_files = sorted(all_files + encrypted_files_flag, key=lambda x: x[0])

    for address, argfile, encrypted in all_files:
        compress = compress

        # Check whether we can compress the current file before flashing
        if compress and encrypted:
            log.print("\n")
            log.warning("Compress and encrypt options are mutually exclusive.")
            log.print(f"Will flash {argfile.name} uncompressed.")
            compress = False

        image = argfile.read()

        if len(image) == 0:
            log.warning(f"File {argfile.name} is empty")
            continue

        image = pad_to(image, esp.FLASH_ENCRYPTED_WRITE_ALIGN if encrypted else 4)

        if not esp.IS_STUB:
            log.print("Erasing flash...")

            # It is not possible to write to not aligned addresses without stub,
            # so there are added 0xFF (erase) bytes at the beginning of the image
            # to align it.
            bytes_over = address % esp.FLASH_SECTOR_SIZE
            address -= bytes_over
            image = b"\xff" * bytes_over + image

        if not esp.secure_download_mode and not esp.get_secure_boot_enabled():
            image = _update_image_flash_params(
                esp, address, flash_freq, flash_mode, flash_size, image
            )
        else:
            log.warning(
                "Security features enabled, so not changing any flash settings."
            )
        calcmd5 = hashlib.md5(image).hexdigest()
        uncsize = len(image)
        if compress:
            uncimage = image
            image = zlib.compress(uncimage, 9)
        original_image = image  # Save the whole image in case retry is needed
        # Try again if reconnect was successful
        for attempt in range(1, esp.WRITE_FLASH_ATTEMPTS + 1):
            try:
                if compress:
                    # Decompress the compressed binary a block at a time,
                    # to dynamically calculate the timeout based on the real write size
                    decompress = zlib.decompressobj()
                    blocks = esp.flash_defl_begin(uncsize, len(image), address)
                else:
                    blocks = esp.flash_begin(
                        uncsize, address, begin_rom_encrypted=encrypted
                    )
                argfile.seek(0)  # in case we need it again
                seq = 0
                bytes_sent = 0  # bytes sent on wire
                bytes_written = 0  # bytes written to flash
                t = time.time()

                timeout = DEFAULT_TIMEOUT

                log.set_progress(0)

                while len(image) > 0:
                    percent = 100 * (seq + 1) // blocks
                    log.set_progress(percent)
                    if not no_progress:
                        log.print_overwrite(
                            f"Writing at {address + bytes_written:010x}... "
                            f"({percent} %)"
                        )
                    block = image[0 : esp.FLASH_WRITE_SIZE]
                    if compress:
                        # feeding each compressed block into the decompressor lets us
                        # see block-by-block how much will be written
                        block_uncompressed = len(decompress.decompress(block))
                        bytes_written += block_uncompressed
                        block_timeout = max(
                            DEFAULT_TIMEOUT,
                            timeout_per_mb(
                                ERASE_WRITE_TIMEOUT_PER_MB, block_uncompressed
                            ),
                        )
                        if not esp.IS_STUB:
                            # ROM code writes block to flash before ACKing
                            timeout = block_timeout
                        esp.flash_defl_block(block, seq, timeout=timeout)
                        if esp.IS_STUB:
                            # Stub ACKs when block is received,
                            # then writes to flash while receiving the block after it
                            timeout = block_timeout
                    else:
                        # Pad the last block
                        block = block + b"\xff" * (esp.FLASH_WRITE_SIZE - len(block))
                        if encrypted:
                            esp.flash_encrypt_block(block, seq)
                        else:
                            esp.flash_block(block, seq)
                        bytes_written += len(block)
                    bytes_sent += len(block)
                    image = image[esp.FLASH_WRITE_SIZE :]
                    seq += 1
                break
            except SerialException:
                if attempt == esp.WRITE_FLASH_ATTEMPTS or encrypted:
                    # Already retried once or encrypted mode is disabled because of
                    # security reasons
                    raise
                log.print("\nLost connection, retrying...")
                esp._port.close()
                log.print("Waiting for the chip to reconnect", end="")
                for _ in range(DEFAULT_CONNECT_ATTEMPTS):
                    try:
                        time.sleep(1)
                        esp._port.open()
                        log.print()  # Print new line which was suppressed by print(".")
                        esp.connect()
                        if esp.IS_STUB:
                            # Hack to bypass the stub overwrite check
                            esp.IS_STUB = False
                            # Reflash stub because chip was reset
                            esp = esp.run_stub()
                        image = original_image
                        break
                    except SerialException:
                        log.print(".", end="", flush=True)
                else:
                    raise  # Reconnect limit reached

        if esp.IS_STUB:
            # Stub only writes each block to flash after 'ack'ing the receive,
            # so do a final dummy operation which will not be 'ack'ed
            # until the last block has actually been written out to flash
            esp.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR, timeout=timeout)

        t = time.time() - t
        speed_msg = ""
        if compress:
            if t > 0.0:
                speed_msg = f" (effective {uncsize / t * 8 / 1000:.1f} kbit/s)"
            log.print_overwrite(
                f"Wrote {uncsize} bytes ({bytes_sent} compressed) at {address:#010x} "
                f"in {t:.1f} seconds{speed_msg}...",
                last_line=True,
            )
        else:
            if t > 0.0:
                speed_msg = " (%.1f kbit/s)" % (bytes_written / t * 8 / 1000)
            log.print_overwrite(
                f"Wrote {bytes_written} bytes at {address:#010x} in {t:.1f} "
                f"seconds{speed_msg}...",
                last_line=True,
            )

        if not encrypted and not esp.secure_download_mode:
            try:
                res = esp.flash_md5sum(address, uncsize)
                if res != calcmd5:
                    log.print(f"File  MD5: {calcmd5}")
                    log.print(f"Flash MD5: {res}")
                    if res == hashlib.md5(b"\xff" * uncsize).hexdigest():
                        raise FatalError(
                            "Write failed, the written flash region is empty."
                        )
                    raise FatalError("MD5 of file does not match data in flash!")
                else:
                    log.print("Hash of data verified.")
            except NotImplementedInROMError:
                pass
        else:
            log.print(
                "Cannot verify written data if encrypted or in secure download mode."
            )

    if esp.IS_STUB:
        # skip sending flash_finish to ROM loader here,
        # as it causes the loader to exit and run user code
        esp.flash_begin(0, 0)

        # Get the "encrypted" flag for the last file flashed
        # Note: all_files list contains triplets like:
        # (address: Integer, filename: String, encrypted: Boolean)
        last_file_encrypted = all_files[-1][2]

        # Check whether the last file flashed was compressed or not
        if compress and not last_file_encrypted:
            esp.flash_defl_finish(False)
        else:
            esp.flash_finish(False)


def read_mac(esp: ESPLoader) -> None:
    """
    Read and display the MAC address of the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
    """

    def print_mac(label, mac):
        log.print(f"{label}: {':'.join(f'{x:02x}' for x in mac)}")

    eui64 = esp.read_mac("EUI64")
    if eui64:
        print_mac("MAC", eui64)
        print_mac("BASE MAC", esp.read_mac("BASE_MAC"))
        print_mac("MAC_EXT", esp.read_mac("MAC_EXT"))
    else:
        print_mac("MAC", esp.read_mac("BASE_MAC"))


def chip_id(esp: ESPLoader) -> None:
    """
    Read and display the Chip ID of the ESP device if available,
    otherwise fall back to displaying the MAC address.

    Args:
        esp: Initiated esp object connected to a real device.
    """
    try:
        chipid = esp.chip_id()
        log.print(f"Chip ID: {chipid:#010x}")
    except NotSupportedError:
        log.warning(f"{esp.CHIP_NAME} has no Chip ID. Reading MAC instead.")
        read_mac(esp)


def attach_flash(
    esp: ESPLoader,
    spi_connection: (tuple[int, int, int, int, int] | str) | None = None,
) -> None:
    """
    Configure and attach a SPI flash memory chip to the ESP device,
    verify the connection.
    All following flash operations will be performed on the attached flash chip.

    Args:
        esp: Initiated esp object connected to a real device.
        spi_connection: Custom SPI connection configuration.
            This can either be a tuple containing five pin numbers
            ``(CLK, Q, D, HD, CS)`` for manual configuration
            or a string (``"SPI"`` or ``"HSPI"``) representing a pre-defined config.
            If not provided, the default flash connection is used.
    """

    def _define_spi_conn(spi_connection):
        """Prepare SPI configuration string and value for flash_spi_attach()"""
        clk, q, d, hd, cs = spi_connection
        spi_config_txt = f"CLK:{clk}, Q:{q}, D:{d}, HD:{hd}, CS:{cs}"
        value = (hd << 24) | (cs << 18) | (d << 12) | (q << 6) | clk
        return spi_config_txt, value

    # Override the common SPI flash parameter stuff if configured to do so
    if spi_connection is not None:
        spi_config = spi_connection
        if spi_connection == "SPI":
            value = 0
        elif spi_connection == "HSPI":
            value = 1
        else:
            esp.check_spi_connection(spi_connection)
            # Encode the pin numbers as a 32-bit integer with packed 6-bit values,
            # the same way the ESP ROM takes them
            spi_config, value = _define_spi_conn(spi_connection)
        log.print(f"Configuring SPI flash mode ({spi_config})...")
        esp.flash_spi_attach(value)
    elif not esp.IS_STUB:
        if esp.CHIP_NAME != "ESP32" or esp.secure_download_mode:
            log.print("Enabling default SPI flash mode...")
            # ROM loader doesn't enable flash unless we explicitly do it
            esp.flash_spi_attach(0)
        else:
            # ROM doesn't attach in-package flash chips
            spi_chip_pads = esp.get_chip_spi_pads()
            spi_config_txt, value = _define_spi_conn(spi_chip_pads)
            if spi_chip_pads != (0, 0, 0, 0, 0):
                log.print(
                    "Attaching flash from eFuses' SPI pads configuration "
                    f"({spi_config_txt})..."
                )
            else:
                log.print("Enabling default SPI flash mode...")
            esp.flash_spi_attach(value)

    # XMC chip startup sequence
    XMC_VENDOR_ID = 0x20

    def is_xmc_chip_strict():
        # Read ID without cache, because it should be different after the XMC startup
        id = esp.flash_id(cache=False)
        rdid = ((id & 0xFF) << 16) | ((id >> 16) & 0xFF) | (id & 0xFF00)

        vendor_id = (rdid >> 16) & 0xFF
        mfid = (rdid >> 8) & 0xFF
        cpid = rdid & 0xFF

        if vendor_id != XMC_VENDOR_ID:
            return False

        matched = False
        if mfid == 0x40:
            if cpid >= 0x13 and cpid <= 0x20:
                matched = True
        elif mfid == 0x41:
            if cpid >= 0x17 and cpid <= 0x20:
                matched = True
        elif mfid == 0x50:
            if cpid >= 0x15 and cpid <= 0x16:
                matched = True
        return matched

    def flash_xmc_startup():
        # If the RDID value is a valid XMC one, may skip the flow
        fast_check = True
        if fast_check and is_xmc_chip_strict():
            return  # Successful XMC flash chip boot-up detected by RDID, skipping.

        sfdp_mfid_addr = 0x10
        mf_id = esp.read_spiflash_sfdp(sfdp_mfid_addr, 8)
        if mf_id != XMC_VENDOR_ID:  # Non-XMC chip detected by SFDP Read, skipping.
            return

        log.warning(
            "XMC flash chip boot-up failure detected! Running XMC25QHxxC startup flow."
        )
        esp.run_spiflash_command(0xB9)  # Enter DPD
        esp.run_spiflash_command(0x79)  # Enter UDPD
        esp.run_spiflash_command(0xFF)  # Exit UDPD
        time.sleep(0.002)  # Delay tXUDPD
        esp.run_spiflash_command(0xAB)  # Release Power-Down
        time.sleep(0.00002)
        # Check for success
        if not is_xmc_chip_strict():
            log.warning("XMC flash boot-up fix failed.")
        log.print("XMC flash chip boot-up fix successful!")

    # Check if XMC SPI flash chip booted-up successfully, fix if not
    if not esp.secure_download_mode:
        try:
            flash_xmc_startup()
        except FatalError as e:
            esp.trace(f"Unable to perform XMC flash chip startup sequence ({e}).")

    # Check flash chip connection
    if not esp.secure_download_mode:
        try:
            flash_id = esp.flash_id()
            if flash_id in (0xFFFFFF, 0x000000):
                log.warning(
                    "Failed to communicate with the flash chip, "
                    "read/write operations will fail. "
                    "Try checking the chip connections or removing "
                    "any other hardware connected to IOs."
                )
                if spi_connection is not None:
                    log.note(
                        "Some GPIO pins might be used by other peripherals, try using "
                        "another combination of pins for SPI flash connection."
                    )

        except FatalError as e:
            raise FatalError(f"Unable to verify flash chip connection ({e}).")


def _set_flash_parameters(esp, flash_size="keep"):
    """
    Configure the ESP device's flash memory parameters based on the selected flash size.

    Must be called after attach_flash() and before any flash read/write operations.
    It supports three modes of operation based on the flash_size argument:
      - "detect": Automatically detects the flash size
                  (with a fallback to 4MB if detection fails)
      - "keep": Leaves the flash parameters unchanged in the image header,
                but configures the SPI flash chip with its detected size (if possible)
      - Explicit size (e.g., "4MB", "8MB", etc.): Directly uses the specified flash size

    Args:
        esp (ESPLoader): Initiated esp object connected to a real device.
        flash_size (str, optional): The flash size setting to use. Can be "detect",
                                    "keep", or an explicit flash size value
                                    (default: "keep").

    Returns:
        str | Any: Returns "keep" if flash_size was "keep", or the flash size value
                   used for configuration. In "detect" mode, this is the auto-detected
                   flash size (or "4MB" as a fallback).
    """

    log.print("Configuring flash size...")
    keep = flash_size == "keep"

    # Determine flash size
    if flash_size == "detect":
        flash_size = detect_flash_size(esp)
        if flash_size is None:
            log.warning("Could not auto-detect flash size, defaulting to 4MB.")
            flash_size = "4MB"
        else:
            log.print(f"Auto-detected flash size: {flash_size}")
    elif flash_size == "keep":
        # Set flash size will not change in image header,
        # but the flash chip should be configured with the real size if possible
        flash_size = None if esp.secure_download_mode else detect_flash_size(esp)
        if not esp.IS_STUB:
            log.note("In case of failure, please set a specific flash size.")

    # Set flash parameters
    if flash_size is not None:  # Not "keep" in secure download mode
        esp.flash_set_parameters(flash_size_bytes(flash_size))
        # Check if stub supports chosen flash size
        if (
            esp.IS_STUB
            and esp.CHIP_NAME != "ESP32-S3"
            and flash_size_bytes(flash_size) > 16 * 1024 * 1024
        ):
            log.note(
                "Flasher stub doesn't fully support flash size larger "
                "than 16MB, disable it in case of failure."
            )

    return "keep" if keep else flash_size


def erase_flash(esp: ESPLoader, force: bool = False) -> None:
    """
    Erase the SPI flash memory of the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
        force: Bypass the security checks for flash encryption and secure boot.
    """
    if not force and esp.CHIP_NAME != "ESP8266" and not esp.secure_download_mode:
        if esp.get_flash_encryption_enabled() or esp.get_secure_boot_enabled():
            raise FatalError(
                "Active security features detected, "
                "erasing flash is disabled as a safety measure. "
                "Use the force argument to override, "
                "please use with caution, otherwise it may brick your device!"
            )
    log.print("Erasing flash (this may take a while)...")
    if esp.CHIP_NAME != "ESP8266" and not esp.IS_STUB:
        log.note(
            "You can use the erase_region command in ROM bootloader "
            "mode to erase a specific region."
        )
    t = time.time()
    esp.erase_flash()
    log.print(f"Chip erase completed successfully in {time.time() - t:.1f} seconds.")


def erase_region(esp: ESPLoader, address: int, size: int, force: bool = False) -> None:
    """
    Erase a specific region of the SPI flash memory of the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
        address: The starting address from which to begin erasing.
        size: The total number of bytes to erase.
        force: Bypass the security checks for flash encryption and secure boot.
    """
    if address % ESPLoader.FLASH_SECTOR_SIZE != 0:
        raise FatalError(
            f"Offset to erase from must be a multiple of {ESPLoader.FLASH_SECTOR_SIZE}"
        )
    if size % ESPLoader.FLASH_SECTOR_SIZE != 0:
        raise FatalError(
            f"Size of data to erase must be a multiple of {ESPLoader.FLASH_SECTOR_SIZE}"
        )
    if not force and esp.CHIP_NAME != "ESP8266" and not esp.secure_download_mode:
        if esp.get_flash_encryption_enabled() or esp.get_secure_boot_enabled():
            raise FatalError(
                "Active security features detected, "
                "erasing flash is disabled as a safety measure. "
                "Use the force argument to override, "
                "please use with caution, otherwise it may brick your device!"
            )
    log.print("Erasing region (may be slow depending on size)...")
    t = time.time()
    if esp.CHIP_NAME != "ESP8266" and not esp.IS_STUB:
        # flash_begin triggers a flash erase, enabling erasing in ROM and SDM
        esp.flash_begin(size, address, logging=False)
    else:
        esp.erase_region(address, size)
    log.print(f"Erase completed successfully in {time.time() - t:.1f} seconds.")


def run(esp: ESPLoader) -> None:
    """
    Execute the firmware loaded on the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
    """
    esp.run()


def print_flash_id(esp: ESPLoader) -> None:
    """
    Read and display the SPI flash memory chip ID and related information.

    Args:
        esp: Initiated esp object connected to a real device.
    """
    flash_id = esp.flash_id()
    log.print(f"Manufacturer: {flash_id & 0xFF:02x}")
    flid_lowbyte = (flash_id >> 16) & 0xFF
    log.print(f"Device: {(flash_id >> 8) & 0xFF:02x}{flid_lowbyte:02x}")
    log.print(
        f"Detected flash size: {DETECTED_FLASH_SIZES.get(flid_lowbyte, 'Unknown')}"
    )


def flash_id(esp: ESPLoader) -> None:
    """
    Read and display the SPI flash memory chip identification and configuration details,
    such as the manufacturer ID, device ID, detected flash size, type, and voltage.

    Args:
        esp: Initiated esp object connected to a real device.
    """
    print_flash_id(esp)
    flash_type = esp.flash_type()
    flash_type_dict = {0: "quad (4 data lines)", 1: "octal (8 data lines)"}
    flash_type_str = flash_type_dict.get(flash_type)
    if flash_type_str:
        log.print(f"Flash type set in eFuse: {flash_type_str}")
    try:
        esp.get_flash_voltage()
    except NotSupportedError:
        pass  # Ignore if not supported


def read_flash_sfdp(esp: ESPLoader, address: int, bytes: int = 1) -> None:
    """
    Read and display the Serial Flash Discoverable Parameters (SFDP)
    from the flash memory.

    Args:
        esp: Initiated esp object connected to a real device.
        address: Starting address in the SFDP region to read from.
        bytes: Number of bytes to read (1-4).
    """
    if not (1 <= bytes <= 4):
        raise FatalError("Invalid number of bytes to read from SFDP (1-4).")
    print_flash_id(esp)
    sfdp = esp.read_spiflash_sfdp(address, bytes * 8)
    log.print(f"SFDP[{address}..{address + bytes - 1}]: ", end="")
    for _ in range(bytes):
        log.print(f"{sfdp & 0xFF:02X} ", end="")
        sfdp = sfdp >> 8
    log.print()


def read_flash(
    esp: ESPLoader,
    address: int,
    size: int,
    output: str | None = None,
    flash_size: str = "keep",
    no_progress: bool = False,
) -> bytes | None:
    """
    Read a specified region of SPI flash memory of an ESP device
    and optionally save it to a file.

    Args:
        esp: Initiated esp object connected to a real device.
        address: The starting address in flash memory to read from.
        size: The number of bytes to read.
        output: The name of the file to save the read data.
            If None, the function returns the data.
        flash_size: Flash size setting, needs to be set only when
            the stub flasher is disabled.
            Options: ``"detect"``: auto-detect flash size with fallback to 4MB,
            ``"keep"``: auto-detect but skip setting parameters in SDM,
            Explicit size: use the specified flash size.
        no_progress: Disable printing progress.

    Returns:
        The read flash data as bytes if output is None; otherwise,
        returns None after writing to file.
    """
    _set_flash_parameters(esp, flash_size)
    if no_progress:
        flash_progress = None
    else:

        def flash_progress(progress, length):
            percent = progress * 100.0 / length
            log.set_progress(percent)

            msg = f"{progress} ({percent:.0f} %)"
            padding = "\b" * len(msg)

            if progress != length:
                log.print_overwrite(f"{msg}{padding}")
            else:
                log.print_overwrite(f"{msg}", last_line=True)

    log.set_progress(0)
    t = time.time()
    data = esp.read_flash(address, size, flash_progress)
    t = time.time() - t
    speed_msg = " ({:.1f} kbit/s)".format(len(data) / t * 8 / 1000) if t > 0.0 else ""
    log.print_overwrite(
        f"Read {len(data)} bytes at {address:#010x} in {t:.1f} seconds{speed_msg}...",
        last_line=True,
    )
    if output:
        with open(output, "wb") as f:
            f.write(data)
        log.print(f"Flash read to '{output}' completed.")
        return None
    else:
        log.print("Flash read completed.")
        return data


def verify_flash(
    esp: ESPLoader,
    addr_filename: list[tuple[int, BinaryIO]],
    flash_freq: str = "keep",
    flash_mode: str = "keep",
    flash_size: str = "keep",
    diff: bool = False,
) -> None:
    """
    Verify the contents of the SPI flash memory against the provided binary files.

    Args:
        esp: Initiated esp object connected to a real device.
        addr_filename: List of (address, file) tuples specifying what
            parts of flash memory to verify.
        flash_freq: Flash frequency setting (``"keep"`` to retain current).
        flash_mode: Flash mode setting (``"keep"`` to retain current).
        flash_size: Flash size setting (``"keep"`` to retain current).
        diff: If True, perform a byte-by-byte comparison on failure.
    """
    flash_size = _set_flash_parameters(esp, flash_size)  # Set flash size parameters
    mismatch = False

    for address, argfile in addr_filename:
        image = pad_to(argfile.read(), 4)
        argfile.seek(0)  # rewind in case we need it again

        image = _update_image_flash_params(
            esp, address, flash_freq, flash_mode, flash_size, image
        )

        image_size = len(image)
        log.print(
            f"Verifying {image_size:#x} ({image_size}) bytes "
            f"at {address:#010x} in flash against {argfile.name}..."
        )
        # Try digest first, only read if there are differences.
        digest = esp.flash_md5sum(address, image_size)
        expected_digest = hashlib.md5(image).hexdigest()
        if digest == expected_digest:
            log.print("-- verify OK (digest matched)")
            continue
        else:
            mismatch = True
            if not diff:
                log.print("-- verify FAILED (digest mismatch)")
                continue

        flash = esp.read_flash(address, image_size)
        assert flash != image
        differences = [i for i in range(image_size) if flash[i] != image[i]]
        log.print(
            f"-- verify FAILED: {len(differences)} differences, "
            f"first at {address + differences[0]:#010x}"
        )
        for d in differences:
            flash_byte = flash[d]
            image_byte = image[d]
            log.print(f"   {address + d:08x} {flash_byte:02x} {image_byte:02x}")
    if mismatch:
        raise FatalError("Verify failed.")


def read_flash_status(esp: ESPLoader, bytes: int = 2) -> None:
    """
    Read and print the status register value of the SPI flash memory.

    Args:
        esp: Initiated esp object connected to a real device.
        bytes: Number of bytes to read.
    """
    log.print(f"Status value: {esp.read_status(bytes):#06x}")


def write_flash_status(
    esp: ESPLoader, value: int, bytes: int = 2, non_volatile: bool = False
) -> None:
    """
    Write a new value to the SPI flash memory status register and verify the update.

    Args:
        esp: Initiated esp object connected to a real device.
        value: The new status register value to write.
        bytes: Number of bytes to write.
        non_volatile: If True, allows non-volatile status register bits
            to be written.
    """
    fmt = f"0x%0{bytes * 2}x"
    value = value & ((1 << (bytes * 8)) - 1)
    log.print(f"Initial flash status: {fmt % esp.read_status(bytes)}")
    log.print(f"Setting flash status: {fmt % value}")
    esp.write_status(value, bytes, non_volatile)
    log.print(f"After flash status:   {fmt % esp.read_status(bytes)}")


def get_security_info(esp: ESPLoader) -> None:
    """
    Read and display security-related information about the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
    """
    # The following mapping was taken from the ROM code
    # This mapping is same across all targets in the ROM
    SECURITY_INFO_FLAG_MAP = {
        "SECURE_BOOT_EN": (1 << 0),
        "SECURE_BOOT_AGGRESSIVE_REVOKE": (1 << 1),
        "SECURE_DOWNLOAD_ENABLE": (1 << 2),
        "SECURE_BOOT_KEY_REVOKE0": (1 << 3),
        "SECURE_BOOT_KEY_REVOKE1": (1 << 4),
        "SECURE_BOOT_KEY_REVOKE2": (1 << 5),
        "SOFT_DIS_JTAG": (1 << 6),
        "HARD_DIS_JTAG": (1 << 7),
        "DIS_USB": (1 << 8),
        "DIS_DOWNLOAD_DCACHE": (1 << 9),
        "DIS_DOWNLOAD_ICACHE": (1 << 10),
    }

    # Get the status of respective security flag
    def get_security_flag_status(flag_name, flags_value):
        try:
            return (flags_value & SECURITY_INFO_FLAG_MAP[flag_name]) != 0
        except KeyError:
            raise ValueError(f"Invalid flag name: {flag_name}")

    si = esp.get_security_info()
    log.print()
    title = "Security Information:"
    log.print(title)
    log.print("=" * len(title))
    log.print("Flags: {:#010x} ({})".format(si["flags"], bin(si["flags"])))
    if esp.KEY_PURPOSES:
        log.print(f"Key Purposes: {si['key_purposes']}")
        desc = "\n  ".join(
            [
                f"BLOCK_KEY{key_num} - {esp.KEY_PURPOSES.get(purpose, 'UNKNOWN')}"
                for key_num, purpose in enumerate(si["key_purposes"])
                if key_num <= esp.EFUSE_MAX_KEY
            ]
        )
        log.print(f"  {desc}")
    if si["chip_id"] is not None and si["api_version"] is not None:
        log.print("Chip ID: {}".format(si["chip_id"]))
        log.print("API Version: {}".format(si["api_version"]))

    flags = si["flags"]

    if get_security_flag_status("SECURE_BOOT_EN", flags):
        log.print("Secure Boot: Enabled")
        if get_security_flag_status("SECURE_BOOT_AGGRESSIVE_REVOKE", flags):
            log.print("Secure Boot Aggressive key revocation: Enabled")

        revoked_keys = []
        for i, key in enumerate(
            [
                "SECURE_BOOT_KEY_REVOKE0",
                "SECURE_BOOT_KEY_REVOKE1",
                "SECURE_BOOT_KEY_REVOKE2",
            ]
        ):
            if get_security_flag_status(key, flags):
                revoked_keys.append(i)

        if len(revoked_keys) > 0:
            log.print("Secure Boot Key Revocation Status:\n")
            for i in revoked_keys:
                log.print(f"\tSecure Boot Key{i} is Revoked\n")

    else:
        log.print("Secure Boot: Disabled")

    flash_crypt_cnt = bin(si["flash_crypt_cnt"])
    if (flash_crypt_cnt.count("1") % 2) != 0:
        log.print("Flash Encryption: Enabled")
    else:
        log.print("Flash Encryption: Disabled")

    CRYPT_CNT_STRING = "SPI Boot Crypt Count (SPI_BOOT_CRYPT_CNT)"
    if esp.CHIP_NAME == "esp32":
        CRYPT_CNT_STRING = "Flash Crypt Count (FLASH_CRYPT_CNT)"

    log.print(f"{CRYPT_CNT_STRING}: {si['flash_crypt_cnt']:#x}")

    if get_security_flag_status("DIS_DOWNLOAD_DCACHE", flags):
        log.print("Dcache in UART download mode: Disabled")

    if get_security_flag_status("DIS_DOWNLOAD_ICACHE", flags):
        log.print("Icache in UART download mode: Disabled")

    hard_dis_jtag = get_security_flag_status("HARD_DIS_JTAG", flags)
    soft_dis_jtag = get_security_flag_status("SOFT_DIS_JTAG", flags)
    if hard_dis_jtag:
        log.print("JTAG: Permanently Disabled")
    elif soft_dis_jtag:
        log.print("JTAG: Software Access Disabled")
    if get_security_flag_status("DIS_USB", flags):
        log.print("USB Access: Disabled")


def reset_chip(esp: ESPLoader, reset_mode: str = "hard_reset") -> None:
    """
    Reset the ESP device.

    Args:
        esp: Initiated esp object connected to a real device.
        reset_mode: Reset mode to use (
            ``"hard_reset"``: perform a hard reset using the RTS control line,
            ``"soft_reset"``: perform a soft reset,
            ``"no_reset"``: stay in bootloader,
            ``"no_reset_stub"``: stay in flasher stub,
            ``"watchdog_reset"``: perform a hard reset utilizing a software watchdog.
            )

    """
    if reset_mode == "hard_reset":
        esp.hard_reset()
    elif reset_mode == "soft_reset":
        log.print("Soft resetting...")
        # flash_finish will trigger a soft reset
        esp.soft_reset(False)
    elif reset_mode == "no_reset_stub":
        log.print("Staying in flasher stub.")
    elif reset_mode == "watchdog_reset":
        if esp.secure_download_mode:
            log.warning(
                "Watchdog hard reset is not supported in Secure Download Mode, "
                "attempting classic hard reset instead."
            )
            esp.hard_reset()
        else:
            esp.watchdog_reset()
    elif reset_mode == "no_reset":
        log.print("Staying in bootloader.")
        if esp.IS_STUB:
            esp.soft_reset(True)  # Exit the stub flasher back to ROM loader
    else:
        raise FatalError(f"Invalid reset mode: {reset_mode}")


def run_stub(esp: ESPLoader) -> ESPLoader:
    """
    Load and execute the stub loader on the ESP device. If stub loading
    is not supported or is explicitly disabled, warnings are logged.

    Args:
        esp: Initiated esp object connected to a real device.

    Returns:
        The esp instance, either as a stub child class in a state
        where the stub has been executed, or in its original state
        if the stub loader is disabled or unsupported.
    """
    if esp.secure_download_mode:
        log.warning(
            "Stub loader is not supported in Secure Download Mode, "
            "it has been disabled. Set --no-stub to suppress this warning."
        )
    elif not esp.IS_STUB and esp.stub_is_disabled:
        log.warning(
            "Stub loader has been disabled for compatibility, "
            "set --no-stub to suppress this warning."
        )
    elif esp.CHIP_NAME in [
        "ESP32-H21",
        "ESP32-H4",
    ]:  # TODO: [ESP32H21] IDF-11509   [ESP32H4] IDF-12271
        log.warning(
            f"Stub loader is not yet supported on {esp.CHIP_NAME}, "
            "it has been disabled. Set --no-stub to suppress this warning."
        )
    else:
        try:
            return esp.run_stub()
        except Exception:
            # The CH9102 bridge (PID: 0x55D4) can have issues on MacOS
            if sys.platform == "darwin" and esp._get_pid() == 0x55D4:
                log.print()
                log.note(
                    "If issues persist, "
                    "try installing the WCH USB-to-Serial MacOS driver."
                )
            raise
    return esp


# Commands that don't require an ESP object (image manipulation, etc.)
######################################################################


def _parse_app_info(app_info_segment):
    """
    Check if correct magic word is present in the app_info and parse the app_info struct
    """
    app_info = app_info_segment[:256]
    # More info about the app_info struct can be found at:
    # https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/app_image_format.html#application-description
    APP_DESC_STRUCT_FMT = "<II" + "8s" + "32s32s16s16s32s32sHHB" + "3s" + "72s"
    (
        magic_word,
        secure_version,
        reserv1,
        version,
        project_name,
        time,
        date,
        idf_ver,
        app_elf_sha256,
        min_efuse_blk_rev_full,
        max_efuse_blk_rev_full,
        mmu_page_size,
        reserv3,
        reserv2,
    ) = struct.unpack(APP_DESC_STRUCT_FMT, app_info)

    if magic_word != 0xABCD5432:
        return None

    return {
        "magic_word": magic_word,
        "secure_version": secure_version,
        "reserv1": reserv1,
        "version": sanitize_string(version),
        "project_name": sanitize_string(project_name),
        "time": sanitize_string(time),
        "date": sanitize_string(date),
        "idf_ver": sanitize_string(idf_ver),
        "app_elf_sha256": hexify(app_elf_sha256, uppercase=False),
        "min_efuse_blk_rev_full": (
            f"{min_efuse_blk_rev_full // 100}.{min_efuse_blk_rev_full % 100}"
        ),
        "max_efuse_blk_rev_full": (
            f"{max_efuse_blk_rev_full // 100}.{max_efuse_blk_rev_full % 100}"
        ),
        "mmu_page_size": (
            f"{2**mmu_page_size // 1024} KB" if mmu_page_size != 0 else None
        ),
        "reserv3": reserv3,
        "reserv2": reserv2,
    }


def _parse_bootloader_info(bootloader_info_segment):
    """
    Check if correct magic byte is present in the bootloader_info and parse
    the bootloader_info struct
    """
    bootloader_info = bootloader_info_segment[:80]
    # More info about the bootloader_info struct can be found at:
    # https://docs.espressif.com/projects/esp-idf/en/latest/api-reference/system/bootloader_image_format.html#bootloader-description
    BOOTLOADER_DESC_STRUCT_FMT = "<B" + "3s" + "I32s24s" + "16s"
    (
        magic_byte,
        reserv1,
        version,
        idf_ver,
        date_time,
        reserv2,
    ) = struct.unpack(BOOTLOADER_DESC_STRUCT_FMT, bootloader_info)

    if magic_byte != 0x50:
        return None

    return {
        "magic_byte": magic_byte,
        "reserv1": reserv1,
        "version": version,
        "idf_ver": sanitize_string(idf_ver),
        "date_time": sanitize_string(date_time),
        "reserv2": reserv2,
    }


def image_info(filename: str, chip: str = "auto") -> None:
    """
    Display detailed information about an ESP firmware image.

    Args:
        filename: Path to the firmware image file.
        chip: Target ESP device type (e.g., ``"esp32"``). If set to "auto", the chip
            type will be automatically detected from the image header.
    """
    log.print(f"File size: {get_file_size(filename)} (bytes)")
    with open(filename, "rb") as f:
        # magic number
        try:
            common_header = f.read(8)
            magic = common_header[0]
        except IndexError:
            raise FatalError("File is empty")
        if magic not in [
            ESPLoader.ESP_IMAGE_MAGIC,
            ESP8266V2FirmwareImage.IMAGE_V2_MAGIC,
        ]:
            raise FatalError(
                f"This is not a valid image (invalid magic number: {magic:#x})"
            )

        if chip == "auto":
            try:
                extended_header = f.read(16)

                # append_digest, either 0 or 1
                if extended_header[-1] not in [0, 1]:
                    raise FatalError("Append digest field not 0 or 1")

                chip_id = int.from_bytes(extended_header[4:5], "little")
                for rom in [n for n in ROM_LIST if n.CHIP_NAME != "ESP8266"]:
                    if chip_id == rom.IMAGE_CHIP_ID:
                        chip = rom.CHIP_NAME
                        break
                else:
                    raise FatalError(f"Unknown image chip ID ({chip_id})")
            except FatalError:
                chip = "esp8266"

            log.print(f"Detected image type: {chip.upper()}")

    image = LoadFirmwareImage(chip, filename)

    def get_key_from_value(dict, val):
        """Get key from value in dictionary"""
        for key, value in dict.items():
            if value == val:
                return key
        return None

    log.print()
    title = f"{chip.upper()} image header".format()
    log.print(title)
    log.print("=" * len(title))
    log.print(f"Image version: {image.version}")
    log.print(
        f"Entry point: {image.entrypoint:#8x}"
        if image.entrypoint != 0
        else "Entry point not set"
    )

    log.print(f"Segments: {len(image.segments)}")

    # Flash size
    flash_s_bits = image.flash_size_freq & 0xF0  # high four bits
    flash_s = get_key_from_value(image.ROM_LOADER.FLASH_SIZES, flash_s_bits)
    if flash_s is not None:
        log.print(f"Flash size: {flash_s}")
    else:
        log.warning(f"Invalid flash size ({flash_s_bits:#02x})")

    # Flash frequency
    flash_fr_bits = image.flash_size_freq & 0x0F  # low four bits
    flash_fr = get_key_from_value(image.ROM_LOADER.FLASH_FREQUENCY, flash_fr_bits)
    if flash_fr is not None:
        log.print(f"Flash freq: {flash_fr}")
    else:
        log.warning(f"Invalid flash frequency ({flash_fr_bits:#02x})")

    # Flash mode
    flash_mode = get_key_from_value(FLASH_MODES, image.flash_mode)
    if flash_mode is not None:
        log.print(f"Flash mode: {flash_mode.upper()}")
    else:
        log.warning(f"Invalid flash mode ({image.flash_mode})")

    # Extended header (ESP32 and later only)
    if chip != "esp8266":
        log.print()
        title = f"{chip.upper()} extended image header"
        log.print(title)
        log.print("=" * len(title))
        log.print(
            f"WP pin: {image.wp_pin:#02x}",
            *["(disabled)"] if image.wp_pin == image.WP_PIN_DISABLED else [],
        )
        log.print(
            "Flash pins drive settings: "
            "clk_drv: {:#02x}, q_drv: {:#02x}, d_drv: {:#02x}, "
            "cs0_drv: {:#02x}, hd_drv: {:#02x}, wp_drv: {:#02x}".format(
                image.clk_drv,
                image.q_drv,
                image.d_drv,
                image.cs_drv,
                image.hd_drv,
                image.wp_drv,
            )
        )
        try:
            chip_class = next(
                chip
                for chip in CHIP_DEFS.values()
                if getattr(chip, "IMAGE_CHIP_ID", None) == image.chip_id
            )
            log.print(f"Chip ID: {image.chip_id} ({chip_class.CHIP_NAME})")
        except StopIteration:
            log.print(f"Chip ID: {image.chip_id} (Unknown ID)")
        log.print(
            "Minimal chip revision: "
            f"v{image.min_rev_full // 100}.{image.min_rev_full % 100}, "
            f"(legacy min_rev = {image.min_rev})"
        )
        log.print(
            "Maximal chip revision: "
            f"v{image.max_rev_full // 100}.{image.max_rev_full % 100}"
        )
    log.print()

    # Segments overview
    title = "Segments information"
    log.print(title)
    log.print("=" * len(title))
    headers_str = "{:>7}  {:>7}  {:>10}  {:>10}  {:10}"
    log.print(
        headers_str.format(
            "Segment", "Length", "Load addr", "File offs", "Memory types"
        )
    )
    log.print(f"{'-' * 7}  {'-' * 7}  {'-' * 10}  {'-' * 10}  {'-' * 12}")
    format_str = "{:7}  {:#07x}  {:#010x}  {:#010x}  {}"
    app_desc_seg = None
    bootloader_desc_seg = None
    for idx, seg in enumerate(image.segments):
        segs = seg.get_memory_type(image)
        seg_name = ", ".join(segs)
        # The DROM segment starts with the esp_app_desc_t struct
        if "DROM" in segs and app_desc_seg is None:
            app_desc_seg = seg.data
        elif "DRAM" in segs:
            # The DRAM segment starts with the esp_bootloader_desc_t struct
            if len(seg.data) >= 80:
                bootloader_desc_seg = seg.data
        log.print(
            format_str.format(idx, len(seg.data), seg.addr, seg.file_offs, seg_name)
        )
    log.print()

    # Footer
    title = f"{chip.upper()} image footer"
    log.print(title)
    log.print("=" * len(title))
    calc_checksum = image.calculate_checksum()
    log.print(
        "Checksum: {:#04x} ({})".format(
            image.checksum,
            (
                "valid"
                if image.checksum == calc_checksum
                else f"invalid - calculated {calc_checksum:#04x}"
            ),
        )
    )
    try:
        digest_msg = "Not appended"
        if image.append_digest:
            is_valid = image.stored_digest == image.calc_digest
            digest_msg = "{} ({})".format(
                hexify(image.calc_digest, uppercase=False),
                "valid" if is_valid else "invalid",
            )
            log.print(f"Validation hash: {digest_msg}")
    except AttributeError:
        pass  # ESP8266 image has no append_digest field

    if app_desc_seg:
        app_desc = _parse_app_info(app_desc_seg)
        if app_desc:
            log.print()
            title = "Application information"
            log.print(title)
            log.print("=" * len(title))
            log.print(f"Project name: {app_desc['project_name']}")
            log.print(f"App version: {app_desc['version']}")
            log.print(f"Compile time: {app_desc['date']} {app_desc['time']}")
            log.print(f"ELF file SHA256: {app_desc['app_elf_sha256']}")
            log.print(f"ESP-IDF: {app_desc['idf_ver']}")
            log.print(
                f"Minimal eFuse block revision: {app_desc['min_efuse_blk_rev_full']}"
            )
            log.print(
                f"Maximal eFuse block revision: {app_desc['max_efuse_blk_rev_full']}"
            )
            if app_desc["mmu_page_size"]:
                log.print(f"MMU page size: {app_desc['mmu_page_size']}")
            log.print(f"Secure version: {app_desc['secure_version']}")

    elif bootloader_desc_seg:
        bootloader_desc = _parse_bootloader_info(bootloader_desc_seg)
        if bootloader_desc:
            log.print()
            title = "Bootloader information"
            log.print(title)
            log.print("=" * len(title))
            log.print(f"Bootloader version: {bootloader_desc['version']}")
            log.print(f"ESP-IDF: {bootloader_desc['idf_ver']}")
            log.print(f"Compile time: {bootloader_desc['date_time']}")


def merge_bin(
    addr_filename: list[tuple[int, BinaryIO]],
    output: str,
    chip: str,
    flash_freq: str = "keep",
    flash_mode: str = "keep",
    flash_size: str = "keep",
    format: str = "raw",
    **kwargs,
) -> None:
    """
    Merge multiple binary files into a single output file for flashing to an ESP device.

    Take multiple binary files along with their flash addresses and merge them
    into a unified binary in either raw, UF2, or Intel HEX format.
    Also apply necessary flash parameters and ensure correct alignment for flashing.

    Args:
        addr_filename: List of (address, file) pairs specifying
            memory offsets and corresponding binary files.
        output: Path to the output file where the merged binary will be written.
        chip: Target ESP device type (e.g., ``"esp32"``).
        flash_freq: Flash frequency to set in the image header
            (``"keep"`` to retain current).
        flash_mode: Flash mode to set in the image header
            (``"keep"`` to retain current).
        flash_size: Flash size to set in the image header
            (``"keep"`` to retain current).
        format: Output format (``"raw"``, ``"uf2"``, or ``"hex"``).

    Keyword Args:
        target_offset (int): Starting offset for the merged output.
        pad_to_size (str | None): If specified, pad the output to a specific flash size.
        chunk_size (int | None): Chunk size for UF2 format.
        md5_disable (bool): If True, disable MD5 checks in UF2 format.
    """

    # Set default values of optional arguments
    target_offset: int = kwargs.get("target_offset", 0)
    pad_to_size: str | None = kwargs.get("pad_to_size", None)
    chunk_size: int | None = kwargs.get("chunk_size", None)
    md5_disable: bool = kwargs.get("md5_disable", False)

    try:
        chip_class = CHIP_DEFS[chip]
    except KeyError:
        msg = (
            "Please specify the chip argument"
            if chip == "auto"
            else f"Invalid chip choice: '{chip}'"
        )
        msg = f"{msg} (choose from {', '.join(CHIP_LIST)})"
        raise FatalError(msg)

    # sort the files by offset.
    # The AddrFilenamePairAction has already checked for overlap
    input_files = sorted(addr_filename, key=lambda x: x[0])
    if not input_files:
        raise FatalError("No input files specified")
    first_addr = input_files[0][0]
    if first_addr < target_offset:
        raise FatalError(
            f"Output file target offset is {target_offset:#x}. "
            f"Input file offset {first_addr:#x} is before this."
        )

    if format == "uf2":
        with UF2Writer(
            chip_class.UF2_FAMILY_ID,
            output,
            chunk_size,
            md5_enabled=not md5_disable,
        ) as writer:
            for addr, argfile in input_files:
                log.print(f"Adding {argfile.name} at {addr:#x}")
                image = argfile.read()
                image = _update_image_flash_params(
                    chip_class, addr, flash_freq, flash_mode, flash_size, image
                )
                writer.add_file(addr, image)
        log.print(
            f"Wrote {os.path.getsize(output):#x} bytes to file {output}, "
            f"ready to be flashed with any ESP USB Bridge"
        )

    elif format == "raw":
        with open(output, "wb") as of:

            def pad_to(flash_offs):
                # account for output file offset if there is any
                of.write(b"\xff" * (flash_offs - target_offset - of.tell()))

            for addr, argfile in input_files:
                pad_to(addr)
                image = argfile.read()
                image = _update_image_flash_params(
                    chip_class, addr, flash_freq, flash_mode, flash_size, image
                )
                of.write(image)
            if pad_to_size:
                pad_to(flash_size_bytes(pad_to_size))
            log.print(
                f"Wrote {of.tell():#x} bytes to file {output}, "
                f"ready to flash to offset {target_offset:#x}"
            )

    elif format == "hex":
        out = IntelHex()
        if len(input_files) == 1:
            log.warning(
                "Only one input file specified, output may include "
                "additional padding if input file was previously merged. "
                "Please refer to the documentation for more information: "
                "https://docs.espressif.com/projects/esptool/en/latest/esptool/basic-commands.html#hex-output-format"  # noqa E501
            )
        for addr, argfile in input_files:
            ihex = IntelHex()
            image = argfile.read()
            image = _update_image_flash_params(
                chip_class, addr, flash_freq, flash_mode, flash_size, image
            )
            ihex.frombytes(image, addr)
            out.merge(ihex)
        out.write_hex_file(output)
        log.print(
            f"Wrote {os.path.getsize(output):#x} bytes to file {output}, "
            f"ready to flash to offset {target_offset:#x}"
        )


def elf2image(
    input: str,
    chip: str,
    output: str | None = None,
    flash_freq: str | None = None,
    flash_mode: str = "qio",
    flash_size: str = "1MB",
    **kwargs,
) -> None:
    """
    Convert an ELF file into a firmware image suitable for flashing onto an ESP device.

    Args:
        input: Path to the ELF file.
        chip: Target ESP device type. Defaults to ``"esp8266"`` if set to ``"auto"``.
        output: Path to save the generated firmware image. If None, a default is used.
        flash_freq: Flash frequency to set in the image header.
        flash_mode: Flash mode to set in the image header.
        flash_size: Flash size to set in the image header.

    Keyword Args:
        version (int): ESP8266-only, firmware image version.
        min_rev (int): Minimum chip revision required in legacy format.
        min_rev_full (int): Minimum chip revision required in extended format.
        max_rev_full (int): Maximum chip revision allowed in extended format.
        secure_pad (bool): ESP32-only, enable secure padding.
        secure_pad_v2 (bool): Enable version 2 secure padding.
        elf_sha256_offset (int): Offset for storing the ELF file's SHA-256 hash.
        append_digest (bool): Whether to append a digest to the firmware image.
        use_segments (bool): Use ELF segments instead of sections.
        flash_mmu_page_size (str): MMU page size for flash mapping.
        pad_to_size (str): Pad the final image to a specific flash size.
        ram_only_header (bool): Include only RAM segments and no SHA-256 hash.
    """

    # Set default values of optional arguments
    version: int = kwargs.get("version", 1)
    min_rev: int = kwargs.get("min_rev", 0)
    min_rev_full: int = kwargs.get("min_rev_full", 0)
    max_rev_full: int = kwargs.get("max_rev_full", 65535)
    secure_pad: bool = kwargs.get("secure_pad", False)
    secure_pad_v2: bool = kwargs.get("secure_pad_v2", False)
    elf_sha256_offset: int | None = kwargs.get("elf_sha256_offset", None)
    append_digest: bool = kwargs.get("append_digest", True)
    use_segments: bool = kwargs.get("use_segments", False)
    flash_mmu_page_size: str | None = kwargs.get("flash_mmu_page_size", None)
    pad_to_size: str | None = kwargs.get("pad_to_size", None)
    ram_only_header: bool = kwargs.get("ram_only_header", False)

    e = ELFFile(input)
    if chip == "auto":  # Default to ESP8266 for backwards compatibility
        chip = "esp8266"

    log.print(f"Creating {chip} image...")

    if chip != "esp8266":
        bootloader_image = CHIP_DEFS[chip].BOOTLOADER_IMAGE
        if bootloader_image is None:
            raise FatalError(f"Missing bootloader image definition for {chip}")
        else:
            image = bootloader_image()
        if chip == "esp32" and secure_pad:
            image.secure_pad = "1"
        if secure_pad_v2:
            image.secure_pad = "2"
        image.min_rev = min_rev
        image.min_rev_full = min_rev_full
        image.max_rev_full = max_rev_full
        image.ram_only_header = ram_only_header
        if image.ram_only_header:
            image.append_digest = False
        else:
            image.append_digest = append_digest
    elif version == "1":  # ESP8266
        image = ESP8266ROMFirmwareImage()
    elif version == "2":
        image = ESP8266V2FirmwareImage()
    else:
        image = ESP8266V3FirmwareImage()
    image.entrypoint = e.entrypoint
    image.flash_mode = FLASH_MODES[flash_mode]

    if flash_mmu_page_size:
        image.set_mmu_page_size(flash_size_bytes(flash_mmu_page_size))
    else:
        appdesc_seg = None
        for seg in e.sections:
            if ".flash.appdesc" in seg.name:
                appdesc_seg = seg
                break
        # If ELF file contains an app description segment, which is in the flash memory
        # (RAM build has it too, but does not have MMU page size),
        # and chip has configurable MMU page size.
        if (
            appdesc_seg
            and image.is_flash_addr(appdesc_seg.addr)
            and image.MMU_PAGE_SIZE_CONF
        ):
            app_desc = _parse_app_info(appdesc_seg.data)
            if app_desc:
                # MMU page size is set in app description segment since ESP-IDF v5.4
                if app_desc["mmu_page_size"]:
                    image.set_mmu_page_size(flash_size_bytes(app_desc["mmu_page_size"]))
                # Try to set the correct MMU page size based on the app description
                # starting address which, without image + extended header (24 bytes)
                # and segment header (8 bytes), should be aligned to MMU page size.
                else:
                    for mmu_page_size in reversed(image.MMU_PAGE_SIZE_CONF):
                        if (appdesc_seg.addr - 24 - 8) % mmu_page_size == 0:
                            image.set_mmu_page_size(mmu_page_size)
                            log.print(
                                "MMU page size not specified, set to "
                                f"{image.IROM_ALIGN // 1024} KB"
                            )
                            break
                    else:
                        log.warning(
                            "App description segment is not aligned to MMU page size, "
                            "probably linker script issue or wrong MMU page size. "
                            "Try to set MMU page size parameter manually."
                        )

    # ELFSection is a subclass of ImageSegment, so can use interchangeably
    image.segments = e.segments if use_segments else e.sections
    if pad_to_size:
        image.pad_to_size = flash_size_bytes(pad_to_size)
    image.flash_size_freq = image.ROM_LOADER.parse_flash_size_arg(flash_size)
    image.flash_size_freq += image.ROM_LOADER.parse_flash_freq_arg(flash_freq)

    if elf_sha256_offset:
        image.elf_sha256 = e.sha256()
        image.elf_sha256_offset = elf_sha256_offset
    # If ELF file contains an app_desc section, put the SHA256 digest at correct offset
    elif any(".flash.appdesc" in seg.name for seg in image.segments):
        image.elf_sha256 = e.sha256()
        image.elf_sha256_offset = 0xB0

    if ram_only_header:
        log.print(
            "Image has only RAM segments visible. "
            "ROM segments are hidden and SHA256 digest is not appended."
        )
        image.sort_segments()

    before = len(image.segments)
    image.merge_adjacent_segments()
    if len(image.segments) != before:
        delta = before - len(image.segments)
        log.print(f"Merged {delta} ELF section{'s' if delta > 1 else ''}")

    image.verify()

    if output is None:
        output = image.default_output_name(input)
    image.save(output)

    log.print(f"Successfully created {chip} image.")


def version() -> None:
    """
    Print the current esptool version.
    """
    from . import __version__

    log.print(__version__)
