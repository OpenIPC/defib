"""Grain Media GM8126 recovery over USB.

With no valid header in flash (or with the boot straps at USB) the GM8126 does
not start a camera: it enumerates as a USB mass-storage device, 1233:5677,
that takes Grain Media's own command blocks for the flash. There is no UART
download.

Nothing is uploaded first: the commands below are asked of whatever answers
at that id, and no vendor payload is involved.

Provenance: the command set was read out of Grain Media's Windows flashing
tool and of the ``fsm.bin`` service image shipped with it. **It has not been
tried on a camera.** In particular ``F0 22`` (read) is implemented by fsm.bin
but never issued by the vendor's tool, so the mask ROM may not have it; every
write here depends on reading back first, and refuses when it cannot.
"""
