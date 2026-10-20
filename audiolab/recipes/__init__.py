"""Recipes: content-specific processing presets and the one mastering chain.

params, decisions = resolve('club', profile, quality='standard', advanced={})
report = render(ref_flac, master_flac, params, profile, work_dir, progress)
"""

from .base import RECIPES, ChainParams, Recipe, list_recipes, recipe_for_type, resolve
from .chain import render

__all__ = ['RECIPES', 'ChainParams', 'Recipe', 'list_recipes', 'recipe_for_type', 'resolve', 'render']
