"""TrueNorth Range — Template rendering engine.

Converts range template YAML definitions into Terraform-consumable
variable files, cloud-init user-data, and resource allocation maps.
"""

from .allocator import ResourceAllocator
from .cloud_init import CloudInitGenerator
from .renderer import TemplateRenderer

__all__ = ["TemplateRenderer", "CloudInitGenerator", "ResourceAllocator"]
