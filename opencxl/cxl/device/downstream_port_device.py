"""
 Copyright (c) 2024, Eeum, Inc.

 This software is licensed under the terms of the Revised BSD License.
 See LICENSE for details.
"""

from dataclasses import dataclass
from typing import Optional
from asyncio import create_task, gather

from opencxl.util.logger import logger
from opencxl.cxl.component.common import CXL_COMPONENT_TYPE
from opencxl.cxl.component.cxl_cache_manager import CxlCacheManager
from opencxl.cxl.component.cxl_connection import CxlConnection
from opencxl.cxl.component.virtual_switch.routing_table import RoutingTable
from opencxl.cxl.component.cxl_mem_manager import CxlMemManager
from opencxl.cxl.component.cxl_io_manager import CxlIoManager
from opencxl.cxl.component.cxl_bridge_component import (
    CxlDownstreamPortComponent,
)
from opencxl.cxl.mmio import CombinedMmioRegister, CombinedMmioRegiterOptions
from opencxl.cxl.device.port_device import CxlPortDevice
from opencxl.cxl.device.pci_to_pci_bridge_device import PPBDevice
from opencxl.cxl.config_space.port import (
    CxlDownstreamPortConfigSpace,
    CxlDownstreamPortConfigSpaceOptions,
)
from opencxl.cxl.config_space.dvsec import (
    DvsecConfigSpaceOptions,
    DvsecRegisterLocatorOptions,
    CXL_DEVICE_TYPE,
)
from opencxl.pci.component.pci import (
    PciComponentIdentity,
    PciBridgeComponent,
    PCI_BRIDGE_TYPE,
    PCI_CLASS,
    PCI_BRIDGE_SUBCLASS,
    PCI_DEVICE_PORT_TYPE,
    EEUM_VID,
    SW_DSP_DID,
)
from opencxl.pci.config_space.pci import REG_ADDR
from opencxl.pci.component.mmio_manager import MmioManager, BarEntry
from opencxl.pci.component.config_space_manager import (
    ConfigSpaceManager,
    PCI_DEVICE_TYPE,
)


@dataclass
class DummyConfig:
    vcs_id: int
    vppb_id: int
    routing_table: RoutingTable


@dataclass
class EnumerationInfo:
    secondary_bus: int
    subordinate_bus: int
    memory_base: int
    memory_limit: int


class DownstreamPort(CxlPortDevice):
    def __init__(
        self,
        transport_connection: CxlConnection,
        port_index: int = 0,
        ld_count: int = 1,
    ):
        super().__init__(transport_connection, port_index)

        self._ld_count = ld_count

        self._pci_bridge_component = []
        self._pci_registers = []
        self._cxl_component = []

        self._vppb_upstream_connection = [CxlConnection() for _ in range(self._ld_count)]
        self._vppb_downstream_connection = [CxlConnection() for _ in range(self._ld_count)]        

        self._ppb_device: PPBDevice = None
        self._ppb_bind = None

        self._map_vppb_to_ld = [0 for _ in range(self._ld_count)]

        self._cxl_io_manager = [CxlIoManager(
            self._vppb_upstream_connection[i].mmio_fifo,
            self._vppb_downstream_connection[i].mmio_fifo,
            self._vppb_upstream_connection[i].cfg_fifo,
            self._vppb_downstream_connection[i].cfg_fifo,
            device_type=PCI_DEVICE_TYPE.DOWNSTREAM_BRIDGE,
            init_callback=self._init_device,
            label=self._get_label(),
            is_mld = self._ld_count > 1,
        ) for i in range(self._ld_count)]

        self._cxl_mem_manager = [CxlMemManager(
            upstream_fifo=self._vppb_upstream_connection[i].cxl_mem_fifo,
            downstream_fifo=self._vppb_downstream_connection[i].cxl_mem_fifo,
            label=self._get_label(),
            is_mld = self._ld_count > 1,
        ) for i in range(self._ld_count)]
        self._cxl_cache_manager = [CxlCacheManager(
            upstream_fifo=self._vppb_upstream_connection[i].cxl_cache_fifo,
            downstream_fifo=self._vppb_downstream_connection[i].cxl_cache_fifo,
            label=self._get_label(),
        ) for i in range(self._ld_count)]

    def _init_device(
        self,
        mmio_manager: MmioManager,
        config_space_manager: ConfigSpaceManager,
    ):
        pci_identity = PciComponentIdentity(
            vendor_id=EEUM_VID,
            device_id=SW_DSP_DID,
            base_class_code=PCI_CLASS.BRIDGE,
            sub_class_coce=PCI_BRIDGE_SUBCLASS.PCI_BRIDGE,
            programming_interface=0x00,
            device_port_type=PCI_DEVICE_PORT_TYPE.DOWNSTREAM_PORT_OF_PCI_EXPRESS_SWITCH,
        )
        self._pci_bridge_component.append(PciBridgeComponent(
            identity=pci_identity,
            type=PCI_BRIDGE_TYPE.DOWNSTREAM_PORT,
            mmio_manager=mmio_manager,
            label=self._get_label(),
        ))


        # Create MMIO register
        cxl_component = CxlDownstreamPortComponent()
        self._cxl_component.append(cxl_component)
        mmio_options = CombinedMmioRegiterOptions(cxl_component=cxl_component)
        mmio_register = CombinedMmioRegister(options=mmio_options)
        mmio_manager.set_bar_entries([BarEntry(mmio_register)])

        # Create Config Space Register
        pci_registers_options = CxlDownstreamPortConfigSpaceOptions(
            pci_bridge_component=self._pci_bridge_component[-1],
            dvsec=DvsecConfigSpaceOptions(
                device_type=CXL_DEVICE_TYPE.DSP,
                register_locator=DvsecRegisterLocatorOptions(
                    registers=mmio_register.get_dvsec_register_offsets()
                ),
            ),
        )
        self._pci_registers.append(CxlDownstreamPortConfigSpace(options=pci_registers_options))
        config_space_manager.set_register(self._pci_registers[-1])


    def _get_label(self) -> str:
        return f"DSP{self._port_index}"

    def _create_message(self, message: str) -> str:
        message = f"[{self.__class__.__name__}:{self._get_label()}] {message}"
        return message

    def get_reg_vals(self):
        return self._cxl_io_manager.get_cfg_reg_vals()

    def set_vppb_index(self, vppb_index: int):
        if self._is_dummy:
            raise Exception("Dummy Downstream Port does not support updating the vPPB index")
        self._vppb_index = vppb_index
        self._pci_bridge_component.set_port_number(self._vppb_index)

    def get_device_type(self) -> CXL_COMPONENT_TYPE:
        return CXL_COMPONENT_TYPE.DSP

    def set_routing_table(self, routing_table: RoutingTable):
        if self._is_dummy:
            raise Exception("Dummy Downstream Port does not support updating the routing table")
        self._pci_bridge_component.set_routing_table(routing_table)

    def backup_enumeration_info(self) -> EnumerationInfo:
        info = EnumerationInfo(
            secondary_bus=self._pci_registers.pci.secondary_bus_number,
            subordinate_bus=self._pci_registers.pci.subordinate_bus_number,
            memory_base=self._pci_registers.pci.memory_base,
            memory_limit=self._pci_registers.pci.memory_limit,
        )
        return info

    def get_secondary_bus_number(self):
        return self._pci_registers.pci.secondary_bus_number
    
    def bind_to_vppb(self, ld: int):
        if self._map_vppb_to_ld[ld] == 1:
            raise Exception("No available component to bind")
        self._map_vppb_to_ld[ld] = 1
        return (
            self._cxl_mem_manager[ld], 
            self._cxl_io_manager[ld], 
            self._cxl_cache_manager[ld], 
            self._pci_bridge_component[ld], 
            self._pci_registers[ld], 
            self._cxl_component[ld], 
            self._vppb_upstream_connection[ld], 
            self._vppb_downstream_connection[ld]
        )

    def unbind_from_vppb(self, ld: int):
        if self._map_vppb_to_ld[ld] == 0:
            raise Exception("No component to unbind")
        self._map_vppb_to_ld[ld] = 0



    def restore_enumeration_info(self, info: EnumerationInfo):
        self._pci_registers.write_bytes(
            REG_ADDR.SECONDARY_BUS_NUMBER.START,
            REG_ADDR.SECONDARY_BUS_NUMBER.END,
            info.secondary_bus,
        )
        self._pci_registers.write_bytes(
            REG_ADDR.SUBORDINATE_BUS_NUMBER.START,
            REG_ADDR.SUBORDINATE_BUS_NUMBER.END,
            info.subordinate_bus,
        )
        self._pci_registers.write_bytes(
            REG_ADDR.MEMORY_BASE.START,
            REG_ADDR.MEMORY_BASE.END,
            info.memory_base,
        )
        self._pci_registers.write_bytes(
            REG_ADDR.MEMORY_LIMIT.START,
            REG_ADDR.MEMORY_LIMIT.END,
            info.memory_limit,
        )

    def get_cxl_component(self) -> CxlDownstreamPortComponent:
        return self._cxl_component
    
    def is_sld(self):
        return True
    
    def set_ppb(self, ppb_device: PPBDevice, ppb_bind):
        self._ppb_device = ppb_device
        self._ppb_bind = ppb_bind

    def get_ppb_device(self) -> Optional[PPBDevice]:
        return self._ppb_device

    async def _run(self):
        logger.info(self._create_message("Starting"))
        run_tasks = (
            [create_task(self._cxl_io_manager[i].run()) for i in range(self._ld_count)] +
            [create_task(self._cxl_mem_manager[i].run()) for i in range(self._ld_count)] +
            [create_task(self._cxl_cache_manager[i].run()) for i in range(self._ld_count)]
        )
        wait_tasks = (
            [create_task(self._cxl_io_manager[i].wait_for_ready()) for i in range(self._ld_count)] +
            [create_task(self._cxl_mem_manager[i].wait_for_ready()) for i in range(self._ld_count)] +
            [create_task(self._cxl_cache_manager[i].wait_for_ready()) for i in range(self._ld_count)]
        )
        await gather(*wait_tasks)
        await self._change_status_to_running()
        await gather(*run_tasks)
        logger.info(self._create_message("Stopped"))

    async def _stop(self):
        logger.info(self._create_message("Stopping"))
        tasks = (
            [create_task(self._cxl_io_manager[i].stop()) for i in range(self._ld_count)] +
            [create_task(self._cxl_mem_manager[i].stop()) for i in range(self._ld_count)] +
            [create_task(self._cxl_cache_manager[i].stop()) for i in range(self._ld_count)]
        )
        await gather(*tasks)