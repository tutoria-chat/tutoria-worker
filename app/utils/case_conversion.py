"""
Case conversion utilities for handling different naming conventions
between database (PascalCase) and API responses (snake_case/camelCase).
"""

import re
from typing import Dict, Any, Union


def pascal_to_snake(name: str) -> str:
    """Convert PascalCase to snake_case."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(word.capitalize() for word in components[1:])


def pascal_to_camel(name: str) -> str:
    """Convert PascalCase to camelCase."""
    return name[0].lower() + name[1:] if name else name


def snake_to_pascal(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_"))


def convert_keys(
    data: Union[Dict[str, Any], list], target_case: str = "snake"
) -> Union[Dict[str, Any], list]:
    """
    Recursively convert dictionary keys to specified case.

    Args:
        data: Dictionary or list to convert
        target_case: "snake" for snake_case, "camel" for camelCase

    Returns:
        Data with converted keys
    """
    if isinstance(data, dict):
        new_dict = {}
        for key, value in data.items():
            # Convert the key based on target case
            if target_case == "snake":
                new_key = pascal_to_snake(key)
            elif target_case == "camel":
                new_key = pascal_to_camel(key)
            else:
                new_key = key

            # Recursively convert nested structures
            new_dict[new_key] = convert_keys(value, target_case)
        return new_dict
    elif isinstance(data, list):
        return [convert_keys(item, target_case) for item in data]
    else:
        return data


class CaseConverter:
    """
    Helper class for managing case conversions in API responses.
    """

    @staticmethod
    def to_snake_case(data: Union[Dict[str, Any], list]) -> Union[Dict[str, Any], list]:
        """Convert all keys in data structure to snake_case."""
        return convert_keys(data, "snake")

    @staticmethod
    def to_camel_case(data: Union[Dict[str, Any], list]) -> Union[Dict[str, Any], list]:
        """Convert all keys in data structure to camelCase."""
        return convert_keys(data, "camel")
