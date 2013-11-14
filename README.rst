Badly-missnamed project that provides scripts for setting up ebs
volumes.

This was originally going to be a project that provided buildout
recipes for managing aws resources, but we ended up using
cloudformation, which is slightly less terrifying, instead.

Changes
=======

0.4.0 2013-11-14
----------------

Added support for creating non-RAID10 LVM volumes, mainly for handling
ephemeral disks.

0.3.1 2013-11-11
----------------

Fixed: when not doing RAID, /etc/zim/volumes didn't get renamed.

0.3.0 2013-11-08
----------------

Added support for single non-RAID10 volumes and symbolic links.

