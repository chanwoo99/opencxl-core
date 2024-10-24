"""
 Copyright (c) 2024, Eeum, Inc.

 This software is licensed under the terms of the Revised BSD License.
 See LICENSE for details.
"""

from asyncio import StreamReader, StreamWriter, create_task, gather, Queue, sleep
from dataclasses import dataclass
from enum import StrEnum, IntEnum
from typing import cast, Optional, Dict, Union, List

from opencxl.util.logger import logger
from opencxl.util.component import RunnableComponent
from opencxl.cxl.component.cxl_component import CXL_COMPONENT_TYPE
from opencxl.cxl.component.cxl_connection import CxlConnection
from opencxl.cxl.component.packet_reader import PacketReader
from opencxl.cxl.transport.transaction import (
    BasePacket,
    BaseSidebandPacket,
    CxlIoBasePacket,
    CxlMemBasePacket,
    CxlCacheBasePacket,
    SIDEBAND_TYPES,
    PAYLOAD_TYPE,
    CXL_IO_FMT_TYPE,
)


@dataclass
class FifoGroup:
    cfg_space: Queue
    mmio: Queue
    cxl_mem: Queue
    cxl_cache: Queue

    @staticmethod
    async def merge_queues(target_queue: Queue, source_queues: List[Queue]):
        for queue in source_queues:
            while not queue.empty():
                item = await queue.get()
                await target_queue.put(item)
                queue.task_done()

    @classmethod
    async def merge_groups(cls, groups: List['FifoGroup']) -> 'FifoGroup':
        if len(groups) > 1:
            await gather(
                cls.merge_queues(groups[0].cfg_space, [group.cfg_space for group in groups[1:]]),
                cls.merge_queues(groups[0].mmio, [group.mmio for group in groups[1:]]),
                cls.merge_queues(groups[0].cxl_mem, [group.cxl_mem for group in groups[1:]]),
                #cls.merge_queues(groups[0].cxl_cache, [group.cxl_cache for group in groups[1:]]),
            )



class CXL_IO_FIFO_TYPE(IntEnum):
    CFG = 0
    MMIO = 1


class PROCESSOR_DIRECTION(StrEnum):
    HOST_TO_TARGET = "host to target"
    TARGET_TO_HOST = "target to host"


class CxlPacketProcessor(RunnableComponent):
    def __init__(
        self,
        reader: StreamReader,
        writer: StreamWriter,
        cxl_connection: Union[CxlConnection, List[CxlConnection]],
        component_type: CXL_COMPONENT_TYPE,
        label: Optional[str] = None,
    ):
        super().__init__(label)
        self._reader = PacketReader(reader, label=label)
        self._writer = writer
        self._tlp_table: Dict[(int,int), CXL_IO_FIFO_TYPE] = {}
        self._cxl_connection = cxl_connection
        self._component_type = component_type
        logger.debug(self._create_message(f"Configured for {component_type.name}"))
        if component_type in (CXL_COMPONENT_TYPE.R, CXL_COMPONENT_TYPE.DSP):
            self._incoming = FifoGroup(
                cfg_space=self._cxl_connection.cfg_fifo.target_to_host,
                mmio=self._cxl_connection.mmio_fifo.target_to_host,
                cxl_mem=self._cxl_connection.cxl_mem_fifo.target_to_host,
                cxl_cache=self._cxl_connection.cxl_cache_fifo.target_to_host,
            )
            self._incoming_dir = PROCESSOR_DIRECTION.TARGET_TO_HOST
            self._outgoing = FifoGroup(
                cfg_space=self._cxl_connection.cfg_fifo.host_to_target,
                mmio=self._cxl_connection.mmio_fifo.host_to_target,
                cxl_mem=self._cxl_connection.cxl_mem_fifo.host_to_target,
                cxl_cache=self._cxl_connection.cxl_cache_fifo.host_to_target,
            )
            self._outgoing_dir = PROCESSOR_DIRECTION.HOST_TO_TARGET
        elif component_type in (
            CXL_COMPONENT_TYPE.T1,
            CXL_COMPONENT_TYPE.T2,
            CXL_COMPONENT_TYPE.D2,
            CXL_COMPONENT_TYPE.USP,
        ):
            self._incoming_dir = PROCESSOR_DIRECTION.HOST_TO_TARGET
            self._outgoing_dir = PROCESSOR_DIRECTION.TARGET_TO_HOST

            # Add common FIFOs
            self._incoming = FifoGroup(
                cfg_space=self._cxl_connection.cfg_fifo.host_to_target,
                mmio=self._cxl_connection.mmio_fifo.host_to_target,
                cxl_mem=None,
                cxl_cache=None,
            )

            self._outgoing = FifoGroup(
                cfg_space=self._cxl_connection.cfg_fifo.target_to_host,
                mmio=self._cxl_connection.mmio_fifo.target_to_host,
                cxl_mem=None,
                cxl_cache=None,
            )

            # Add CXL.cache and CXL.mem FIFO based on the device type
            if component_type in (
                CXL_COMPONENT_TYPE.T1,
                CXL_COMPONENT_TYPE.T2,
                CXL_COMPONENT_TYPE.USP,
            ):
                self._incoming.cxl_cache = self._cxl_connection.cxl_cache_fifo.host_to_target
                self._outgoing.cxl_cache = self._cxl_connection.cxl_cache_fifo.target_to_host

            if component_type in (
                CXL_COMPONENT_TYPE.T2,
                CXL_COMPONENT_TYPE.D2,
                CXL_COMPONENT_TYPE.USP,
            ):
                self._incoming.cxl_mem = self._cxl_connection.cxl_mem_fifo.host_to_target
                self._outgoing.cxl_mem = self._cxl_connection.cxl_mem_fifo.target_to_host
        elif component_type == CXL_COMPONENT_TYPE.LD:
            self._incoming_dir = PROCESSOR_DIRECTION.HOST_TO_TARGET
            self._outgoing_dir = PROCESSOR_DIRECTION.TARGET_TO_HOST
            self._incoming = [FifoGroup(
                cfg_space=cxl_conn.cfg_fifo.host_to_target,
                mmio=cxl_conn.mmio_fifo.host_to_target,
                cxl_mem=cxl_conn.cxl_mem_fifo.host_to_target,
                cxl_cache=None,
            ) for cxl_conn in self._cxl_connection]

            self._outgoing = [FifoGroup(
                cfg_space=cxl_conn.cfg_fifo.target_to_host,
                mmio=cxl_conn.mmio_fifo.target_to_host,
                cxl_mem=cxl_conn.cxl_mem_fifo.target_to_host,
                cxl_cache=None,
            ) for cxl_conn in self._cxl_connection]
            
        else:
            raise Exception(f"Unsupported component type {component_type.name}")

    @staticmethod
    def _is_disconnection_notification(packet) -> bool:
        base_packet = cast(BasePacket, packet)
        if base_packet.system_header.payload_type != PAYLOAD_TYPE.SIDEBAND:
            return False
        sideband = cast(BaseSidebandPacket, packet)
        return sideband.sideband_header.type == SIDEBAND_TYPES.CONNECTION_DISCONNECTED

    def _push_tlp_table_entry(self, cxl_io_packet: CxlIoBasePacket):
        tid = cxl_io_packet.get_transaction_id()
        #MLD
        if self._component_type == CXL_COMPONENT_TYPE.LD:
            ld_id = cxl_io_packet.cxl_io_header.ld_id
        else:
            ld_id = -1
        
        if (tid,ld_id) in self._tlp_table:
            raise Exception(f"tid ({tid:02x}) already exists in the TLP table")
        if cxl_io_packet.is_cfg():
            fifo_type = CXL_IO_FIFO_TYPE.CFG
        elif cxl_io_packet.is_mmio():
            fifo_type = CXL_IO_FIFO_TYPE.MMIO
        else:
            fmt_type_str = CXL_IO_FMT_TYPE(cxl_io_packet.cxl_io_header.fmt_type)
            raise Exception(f"pushing tid of {fmt_type_str} type is not allowed")
        self._tlp_table[(tid,ld_id)] = fifo_type

    def _pop_tlp_table_entry(self, cxl_io_packet: CxlIoBasePacket) -> CXL_IO_FIFO_TYPE:
        tid = cxl_io_packet.get_transaction_id()
        #MLD
        if self._component_type == CXL_COMPONENT_TYPE.LD:
            ld_id = cxl_io_packet.cxl_io_header.ld_id
        else:
            ld_id = -1
        if (tid,ld_id) not in self._tlp_table:
            raise Exception(f"tid ({tid:02x}-{ld_id}) is not found in the TLP table")
        fifo_type = self._tlp_table[(tid,ld_id)]

        del self._tlp_table[(tid,ld_id)]
        return fifo_type

    async def _process_incoming_packets(self):
        logger.debug(self._create_message(f"Starting {self._incoming_dir} packet processor"))
        while True:
            try:
                packet = await self._reader.get_packet()
                # io need be modified to supprots MLD
                if packet.is_cxl_io():
                    
                    cxl_io_packet = cast(CxlIoBasePacket, packet)
                    if cxl_io_packet.is_cpl() or cxl_io_packet.is_cpld():
                        logger.debug(
                            self._create_message(
                                f"Received {self._incoming_dir} CXL.io (CPL/CPLD) packet"
                            )
                        )
                        
                        fifo_type = self._pop_tlp_table_entry(cxl_io_packet)
                        #MLD
                        if self._component_type == CXL_COMPONENT_TYPE.LD:
                            ld_id = cxl_io_packet.cxl_io_header.ld_id
                            if fifo_type == CXL_IO_FIFO_TYPE.CFG:
                                await self._incoming[ld_id].cfg_space.put(cxl_io_packet)
                            else:
                                await self._incoming[ld_id].mmio.put(cxl_io_packet)
                        else:
                            if fifo_type == CXL_IO_FIFO_TYPE.CFG:
                                await self._incoming.cfg_space.put(cxl_io_packet)
                            else:
                                await self._incoming.mmio.put(cxl_io_packet)
                    elif cxl_io_packet.is_cfg():
                        logger.debug(
                            self._create_message(
                                f"Received {self._incoming_dir} CXL.io (CFG_RD/CFG_WR) packet"
                            )
                            
                        )
                        self._push_tlp_table_entry(cxl_io_packet)
                        #MLD
                        if self._component_type == CXL_COMPONENT_TYPE.LD:
                            ld_id = cxl_io_packet.cxl_io_header.ld_id
                            await self._incoming[ld_id].cfg_space.put(cxl_io_packet)
                        else:
                            await self._incoming.cfg_space.put(cxl_io_packet)
                    elif cxl_io_packet.is_mmio():
                        logger.debug(
                            self._create_message(
                                f"Received {self._incoming_dir} CXL.io (MRD/MWR) packet"
                            )
                        )
                        if cxl_io_packet.is_mem_write() is False:
                            self._push_tlp_table_entry(cxl_io_packet)
                        #MLD
                        if self._component_type == CXL_COMPONENT_TYPE.LD:
                            ld_id = cxl_io_packet.cxl_io_header.ld_id
                            await self._incoming[ld_id].mmio.put(cxl_io_packet)
                        else:
                            await self._incoming.mmio.put(cxl_io_packet)
                    else:
                        logger.warning(self._create_message("Unexpected CXL.io packet"))
                        logger.debug(self._create_message(packet.get_pretty_string()))
                        raise Exception("Received unexpected CXL.io packet")
                elif packet.is_cxl_mem():
                    if self._component_type != CXL_COMPONENT_TYPE.LD and self._incoming.cxl_mem is None:
                        logger.error(self._create_message("Got CXL.mem packet on no CXL.mem FIFO"))
                        continue
                    logger.debug(
                        self._create_message(f"Received {self._incoming_dir} CXL.mem packet")
                    )
                    cxl_mem_packet = cast(CxlMemBasePacket, packet)
                    if self._component_type == CXL_COMPONENT_TYPE.LD:
                        # LD routing code
                        if cxl_mem_packet.is_m2sreq():
                            ld_id = cxl_mem_packet.m2sreq_header.ld_id
                        elif cxl_mem_packet.is_m2srwd():
                            ld_id = cxl_mem_packet.m2srwd_header.ld_id
                        elif cxl_mem_packet.is_s2mndr():                        
                            ld_id = cxl_mem_packet.s2mndr_header.ld_id
                        elif cxl_mem_packet.is_s2mdrs():
                            ld_id = cxl_mem_packet.s2mdrs_header.ld_id
                        else:
                            logger.warning(self._create_message("Unexpected CXL.mem packet"))

                        await self._incoming[ld_id].cxl_mem.put(cxl_mem_packet)
                    else:                        
                        await self._incoming.cxl_mem.put(cxl_mem_packet)

                elif packet.is_cxl_cache():
                    if self._incoming.cxl_cache is None:
                        logger.error(
                            self._create_message("Got CXL.cache packet on no CXL.cache FIFO")
                        )
                        continue
                    logger.debug(
                        self._create_message(f"Received {self._incoming_dir} CXL.cache packet")
                    )
                    cxl_cache_packet = cast(CxlCacheBasePacket, packet)
                    await self._incoming.cxl_cache.put(cxl_cache_packet)
                else:
                    message = f"Received unexpected {self._incoming_dir} packet"
                    logger.debug(self._create_message(message))
                    raise Exception(message)
            except Exception as e:
                logger.debug(self._create_message(str(e)))
                notification_packet = BaseSidebandPacket.create(
                    SIDEBAND_TYPES.CONNECTION_DISCONNECTED
                )
                await self._notify_outgoing_processors(notification_packet)
                break
        logger.debug(self._create_message(f"Stopped {self._incoming_dir} packet processor"))

    async def _notify_outgoing_processors(self, packet):
        await self._outgoing.cfg_space.put(packet)
        await self._outgoing.mmio.put(packet)
        if self._outgoing.cxl_mem:
            await self._outgoing.cxl_mem.put(packet)
        if self._outgoing.cxl_cache:
            await self._outgoing.cxl_cache.put(packet)

    async def _process_outgoing_cfg_packets(self):
        logger.debug(self._create_message("Starting outgoing CFG FIFO processor"))
        while True:
            if self._component_type == CXL_COMPONENT_TYPE.LD:
                flag = False
                while True:
                    for queue in self._outgoing:
                        if not queue.cfg_space.empty():
                            packet = await queue.cfg_space.get()
                            flag = True           
                            break
                    if flag==True:
                        break
                    await sleep(0.1)
            else:
                packet = await self._outgoing.cfg_space.get()         
                       
            if self._is_disconnection_notification(packet):
                break

            cxl_io_packet = cast(CxlIoBasePacket, packet)
            if cxl_io_packet.is_cpl() or cxl_io_packet.is_cpld():
                logger.debug(
                    self._create_message(f"Received {self._outgoing_dir} CXL.io (CPL/CPLD) packet")
                )
                self._pop_tlp_table_entry(cxl_io_packet)
            else:
                logger.debug(
                    self._create_message(
                        f"Received {self._outgoing_dir} CXL.io (CFG_RD/CFG_WR) packet"
                    )
                )
                self._push_tlp_table_entry(cxl_io_packet)
            self._writer.write(bytes(packet))
            await self._writer.drain()
        logger.debug(self._create_message("Stopped outgoing CFG FIFO processor"))

    async def _process_outgoing_mmio_packets(self):
        logger.debug(self._create_message("Starting outgoing MMIO FIFO processor"))
        while True:
            if self._component_type == CXL_COMPONENT_TYPE.LD:
                flag = False
                while True:
                    for queue in self._outgoing:
                        if not queue.mmio.empty():
                            packet = await queue.mmio.get()
                            flag = True                       
                            break
                    if flag==True:
                        break
                    await sleep(0.1)
            else:
                packet = await self._outgoing.mmio.get()
            if self._is_disconnection_notification(packet):
                break
            cxl_io_packet = cast(CxlIoBasePacket, packet)
            if cxl_io_packet.is_cpl() or cxl_io_packet.is_cpld():
                logger.debug(
                    self._create_message(f"Received {self._outgoing_dir} CXL.io (CPL/CPLD) packet")
                )
                self._pop_tlp_table_entry(cxl_io_packet)
            else:
                logger.debug(
                    self._create_message(f"Received {self._outgoing_dir} CXL.io (MRD/MWR) packet")
                )
                if cxl_io_packet.is_mem_write() is False:
                    self._push_tlp_table_entry(cxl_io_packet)
            self._writer.write(bytes(packet))
            await self._writer.drain()
        logger.debug(self._create_message("Stopped outgoing MMIO FIFO processor"))

    # MLD mem routing
    async def _process_outgoing_cxl_mem_packets(self):
        logger.debug(self._create_message("Starting outgoing CXL.mem FIFO processor"))
        while True:
            if self._component_type == CXL_COMPONENT_TYPE.LD:
                flag = False
                while True:
                    for queue in self._outgoing:
                        if queue.cxl_mem and not queue.cxl_mem.empty():
                            packet = await queue.cxl_mem.get()
                            flag = True                    
                            break
                    if flag==True:
                        break
                    await sleep(0.1)
            else:
                packet = await self._outgoing.cxl_mem.get()
                
            if self._is_disconnection_notification(packet):
                break
            self._writer.write(bytes(packet))
            await self._writer.drain()
        logger.debug(self._create_message("Stopped outgoing CXL.mem FIFO processor"))

    async def _process_outgoing_cxl_cache_packets(self):
        logger.debug(self._create_message("Starting outgoing CXL.cache FIFO processor"))
        while True:
            packet = await self._outgoing.cxl_cache.get()
            if self._is_disconnection_notification(packet):
                break
            self._writer.write(bytes(packet))
            await self._writer.drain()
        logger.debug(self._create_message("Stopped outgoing CXL.cache FIFO processor"))

    async def _process_outgoing_packets(self):
        tasks = [
            create_task(self._process_outgoing_cfg_packets()),
            create_task(self._process_outgoing_mmio_packets()),
        ]
        if self._component_type == CXL_COMPONENT_TYPE.LD:
            for queue in self._outgoing:
                if queue.cxl_mem:
                    tasks.append(create_task(self._process_outgoing_cxl_mem_packets()))      
                    break
            for queue in self._outgoing:
                if queue.cxl_cache:
                    tasks.append(create_task(self._process_outgoing_cxl_cache_packets()))     
                    break              
   

        else:
            if self._outgoing.cxl_mem:
                tasks.append(create_task(self._process_outgoing_cxl_mem_packets()))
            if self._outgoing.cxl_cache:
                tasks.append(create_task(self._process_outgoing_cxl_cache_packets()))
        await gather(*tasks)

    async def _run(self):
        tasks = [
            create_task(self._process_incoming_packets()),
            create_task(self._process_outgoing_packets()),
        ]
        await self._change_status_to_running()
        await gather(*tasks)

    async def _stop(self):
        self._reader.abort()
