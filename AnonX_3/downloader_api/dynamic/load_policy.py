"""Load policy for system protection."""

import logging
from dataclasses import dataclass
from typing import Optional

from AnonX_3.downloader_api.core.constants import ResourceState, MediaType
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor

logger = logging.getLogger(__name__)


@dataclass
class LoadPolicy:
    accept_audio: bool = True
    accept_video: bool = True
    accept_conversion: bool = True
    accept_reencode: bool = True
    max_queue_addition: int = 10
    throttle_rate: float = 1.0


class LoadPolicyManager:
    def __init__(self):
        self.policies = {
            ResourceState.IDLE: LoadPolicy(
                accept_audio=True,
                accept_video=True,
                accept_conversion=True,
                accept_reencode=True,
                max_queue_addition=10,
                throttle_rate=1.0,
            ),
            ResourceState.NORMAL: LoadPolicy(
                accept_audio=True,
                accept_video=True,
                accept_conversion=True,
                accept_reencode=True,
                max_queue_addition=5,
                throttle_rate=1.0,
            ),
            ResourceState.BUSY: LoadPolicy(
                accept_audio=True,
                accept_video=True,
                accept_conversion=True,
                accept_reencode=False,
                max_queue_addition=3,
                throttle_rate=0.8,
            ),
            ResourceState.HIGH_LOAD: LoadPolicy(
                accept_audio=True,
                accept_video=True,
                accept_conversion=False,
                accept_reencode=False,
                max_queue_addition=2,
                throttle_rate=0.5,
            ),
            ResourceState.CRITICAL: LoadPolicy(
                accept_audio=True,
                accept_video=False,
                accept_conversion=False,
                accept_reencode=False,
                max_queue_addition=1,
                throttle_rate=0.2,
            ),
            ResourceState.RECOVERY: LoadPolicy(
                accept_audio=True,
                accept_video=True,
                accept_conversion=True,
                accept_reencode=False,
                max_queue_addition=2,
                throttle_rate=0.6,
            ),
        }

    async def get_policy(
        self,
        resource_state: Optional[ResourceState] = None,
    ) -> LoadPolicy:
        if resource_state is None:
            resource_state = await resource_monitor.get_resource_state()

        return self.policies.get(
            resource_state,
            self.policies[ResourceState.NORMAL],
        )

    async def can_accept(
        self,
        media_type: MediaType,
        needs_conversion: bool = False,
        needs_reencode: bool = False,
    ) -> bool:
        policy = await self.get_policy()

        if media_type == MediaType.VIDEO and not policy.accept_video:
            return False

        if needs_conversion and not policy.accept_conversion:
            return False

        if needs_reencode and not policy.accept_reencode:
            return False

        return True


load_policy_manager = LoadPolicyManager()
