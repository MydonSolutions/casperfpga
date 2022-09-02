import zlib
import hashlib
import logging
import time
import os
import subprocess
import glob
import re
from mmap import mmap, PROT_READ, PROT_WRITE, MAP_SHARED

# AXI -> PCIe offset defined in firmware block diagram. PCI transactions
# on address N get translated to AXI transactions on address N+AXIL_PCI_ADDR_TRANSLATION
AXIL_PCI_ADDR_TRANSLATION = 0x00000000
# Size of AXI-lite memory to map
MAP_SIZE = 8 * 1024 * 1024

from .transport import Transport
from .network import IpAddress
from .utils import socket_closer, parse_fpg

__author__ = "jackh"
__date__ = "May 2020"


class LocalPcieTransport(Transport):
    """
    The transport interface for a locally-connected PCIe FPGA card.
    """

    def __init__(self, **kwargs):
        """
        :param host: The host-device identifier string (see `getXdmaIdFromTarget`)
        :param parent_fpga: Instance of parent_fpga
        :param fpgfile: filepath to fpg image, setting the fpg template
            restriction
        """
        Transport.__init__(self, **kwargs)

        try:
            # Entry point is always via casperfpga.CasperFpga
            self.parent = kwargs["parent_fpga"]
            self.logger = self.parent.logger
        except KeyError:
            errmsg = "parent_fpga argument not supplied when creating skarab"
            # Pointless trying to log to a logger
            raise RuntimeError(errmsg)

        self.instance_id = self.getXdmaIdFromTarget(
            kwargs.get("host", 0), logger=self.logger
        )
        # Local char devices for comms
        self._axil_dev = "/dev/xdma%d_user" % self.instance_id
        # AXI Streaming device for uploading bitstreams
        self._axid_dev = "/dev/xdma%d_h2c_1" % self.instance_id

        new_connection_msg = "*** NEW CONNECTION MADE TO {} ***".format(self.host)
        self.logger.debug(new_connection_msg)
        # Python3 requires opening with no buffering to enable seeking.
        # But... write transactions appear to only work when using the memory map
        # as a byte buffer, i.e. writing as axil_mm[start:stop] = b"....",
        # but not when using as a file, i.e. axil_mm.seek(start); axil_mm.write(b"....").
        # Python2 seems to work with file-like ops.
        self.fh = open(self._axil_dev, "r+b", buffering=0)
        self.axil_mm = mmap(
            self.fh.fileno(), MAP_SIZE, flags=MAP_SHARED, prot=PROT_READ | PROT_WRITE
        )

        self.fpg_template = None
        fpgfile_path = kwargs.get("fpgfile", None)
        if fpgfile_path is not None:
            self.fpg_template = self._parse_template_meta(fpgfile_path)
            self.logger.info("The fpg_template is `{}`.".format(self.fpg_template))
        else:
            self.logger.info("No fpg_template set.")

    def _parse_template_meta(self, fpgfile):
        fpg_header = parse_fpg(fpgfile)[0]
        try:
            return fpg_header[list(fpg_header.keys())[0]]["pr_template"]
        except:
            return None

    def __del__(self):
        self.axil_mm.close()
        self.fh.close()

    def _extract_bitstream(self, filename):
        """
        Extract the header and program bitstream from the input file provided.
        """
        with open(filename, "rb") as fh:
            fpg = fh.read()

        header_offset = fpg.find("\n?quit\n".encode()) + 7
        header = fpg[0:header_offset] + b"0" * (1024 - header_offset % 1024)
        prog = fpg[header_offset:] + b"0" * (1024 - (len(fpg) - header_offset) % 1024)

        if prog.startswith(b"\x1f\x8b\x08"):
            prog = zlib.decompress(prog, 16 + zlib.MAX_WBITS)

        chksum = hashlib.md5()
        chksum.update(fpg)

        return header, prog, chksum.hexdigest()

    def is_connected(self, timeout=None, retries=None):
        """
        'ping' the board to see if it is connected and running.
        Tries to read a register

        :return: Boolean - True/False - Succes/Fail
        """
        if timeout is None:
            timeout = self.timeout
        if retries is None:
            retries = self.retries

        try:
            data = self.read(AXIL_PCI_ADDR_TRANSLATION, 4)
            return True
        except:
            return False

    @staticmethod
    def get_pcie_xdma_map():
        pcie_xdma_regex = r"/sys/bus/pci/drivers/xdma/\d+:(?P<pci_id>.*?):.*?/xdma/xdma(?P<xdma_id>\d+)_user"
        xdma_dev_filepaths = glob.glob("/sys/bus/pci/drivers/xdma/*/xdma/xdma*_user")
        ret = {}
        for fp in xdma_dev_filepaths:
            match = re.match(pcie_xdma_regex, fp)
            ret[match.group("pci_id")] = int(match.group("xdma_id"))
        return ret

    @staticmethod
    def getXdmaIdFromTarget(target, pcie_xdma_map=None, logger=None):
        """
        :param target: Identifier of target PCIe device [pcieAB, xdmaXX, or direct xdma ID].
        """
        if pcie_xdma_map is None:
            if logger is not None:
                logger.info("Generating local pcie_xdma_map")
            pcie_xdma_dict = LocalPcieTransport.get_pcie_xdma_map()
        else:
            if logger is not None:
                logger.info("Using supplied pcie_xdma_map")
            pcie_xdma_dict = pcie_xdma_map

        if isinstance(target, str):
            if target.startswith("pcie"):
                if logger is not None:
                    logger.info("Target supplied with pcie id, mapping to xdma id")
                pci_id = target[4:]
                if pci_id in pcie_xdma_dict:
                    return pcie_xdma_dict[pci_id]

            if target.startswith("xdma"):
                if logger is not None:
                    logger.info("Target supplied with xdma id, mapping to pcie id")
                if int(target[4:]) in pcie_xdma_dict.values():
                    return int(target[4:])

        try:
            if int(target) in pcie_xdma_dict.values():
                return int(target)
        except:  # target was not an integer
            pass

        raise RuntimeError(
            (
                'Specified target "{}" not recognised:\n\tmust begin with either "pcie"'
                ' or "xdma" or be an exact XDMA ID ({}).'
            ).format(
                target,
                {f"pcie{pcie}": f"xdma{xdma}" for pcie, xdma in pcie_xdma_dict.items()},
            )
        )

    def is_running(self):
        """
        Is the FPGA programmed and running a toolflow image?

        *** Not yet implemented ***

        :return: True or False
        """
        return True

    def _get_device_address(self, device_name):
        # map device name to address, if can't find, bail
        if self.memory_devices and (device_name in self.memory_devices):
            return self.memory_devices[device_name].address
        elif (type(device_name) == int) and (0 <= device_name < 2**32):
            # also support absolute address values
            self.logger.warning("Absolute address given: 0x%06x" % device_name)
            return device_name
        errmsg = "Could not find device: %s" % device_name
        self.logger.error(errmsg)
        raise ValueError(errmsg)

    def read(self, device_name, size, offset=0):
        """
        Read size-bytes of binary data.

        :param device_name: name of memory device from which to read
        :param size: how many bytes to read
        :param offset: start at this offset, offset in bytes
        :return: binary data string
        """
        addr = (
            self._get_device_address(device_name) - AXIL_PCI_ADDR_TRANSLATION + offset
        )
        return self.axil_mm[addr : addr + size]

    def blindwrite(self, device_name, data, offset=0):
        """
        Unchecked data write.

        :param device_name: the memory device to which to write
        :param data: the byte string to write
        :param offset: the offset, in bytes, at which to write
        """

        size = len(data)
        assert type(data) == bytes, "Must supply binary data"
        assert size % 4 == 0, "Must write 32-bit-bounded words"
        assert offset % 4 == 0, "Must write 32-bit-bounded words"

        addr = (
            self._get_device_address(device_name) - AXIL_PCI_ADDR_TRANSLATION + offset
        )
        # Write in 4096 byte chunks. Why do i get errors with larger writes?
        written = 0
        block_size = 4096
        for i in range(size // block_size + 1):
            n_bytes = min(size - written, block_size)
            if n_bytes > 0:
                self.axil_mm[addr + written : addr + written + n_bytes] = data[
                    written : written + n_bytes
                ]
            written += n_bytes

    def upload_to_ram_and_program(self, filename, wait_complete=None):
        """
        Uploads an FPGA PR image over PCIe. This image _must_ have been
        generated using an appropriate partial reconfiguration flow.
        """
        assert filename.endswith(".fpg")
        template = self._parse_template_meta(filename)
        if self.fpg_template is not None:
            # assert that the file's template matches
            if self.fpg_template != template:
                raise RuntimeError(
                    (
                        "Programmed image is "
                        "templated by `{}`. Supplied image has mismatching template"
                        "`{}`"
                    ).format(self.fpg_template, template)
                )

        header, bitstream, md5 = self._extract_bitstream(filename)

        with open(self._axid_dev, "wb") as fh:
            fh.write(bitstream)

        # size = len(bitstream)
        # binfile_temp = '/tmp/%s.bin' % os.path.basename(filename)
        # with open(binfile_temp, 'wb') as fh:
        #    fh.write(bitstream)

        # subprocess.run(['dma_to_device', '-d', self._axid_dev, '-s', str(size), '-c', '1', '-f', binfile_temp], check=True)

        if self.fpg_template is None and template is not None:
            self.fpg_template = template

        return True
