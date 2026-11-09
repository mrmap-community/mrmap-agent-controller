"""MrMap Agent Controller.

A conservative controller that turns human-approved GitHub issues into AO worker
sessions and follows the resulting pull requests through CI and human review.

Standard library only. Every external effect goes through :class:`Runner`, which
holds a structural deny list: merging, approving, resolving review threads,
force pushing, label writes and global Docker cleanup are impossible through this
controller, not merely discouraged in a prompt.
"""

__version__ = '2.0.0'
