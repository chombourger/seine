# seine: Singular Embedded Images Now Easy

## Introduction

seine is a command-line tool to build images for embedded systems based on the
Debian operating system. The system is specified in YAML (either as a single
file or several) and may include Ansible playbooks to install packages or
configure them.

The tool was designed to not require elevated privileges after its installation
(sudo isn't used or required, no bind mounts, etc.). The root file-system is
first assembled in a container (seine uses podman because it is daemon-less and
very similar to docker). It is then exported as a tarball and a Linux kernel
started as a user-mode process to create the disk images including partitions
and logical volumes that were specified. Installation of the boot-loader also
happens there since it may require disks/partitions to be created.

## Getting started

### Installation

The easiest way to get started is to install the various packages to your Ubuntu
18.04 host using the following ppa:

```
sudo add-apt-repository ppa:chombourger/ppa
sudo apt-get update
sudo apt-get install podman user-mode-linux
```

You may then either use seine in place (use the seine.py script from the top
level directory of this source tree) or generate a binary package. To build
a sample image without installing seine on your system, use:

```
./seine.py build tests/buster.yaml tests/amd64.yaml tests/test.yaml
```

To produce a binary package, use the dpkg-buildpackage command as follows:

```
sudo mk-buyild-deps -i -r
dpkg-buildpackage -b -uc
```

And install it with:

```
sudo dpkg -i ../seine_0.1-1_all.deb
```

The `seine` tool should then be usable from anywhere (since installed in
`/usr/bin/`) and used as follows:

```
seine build spec.yaml
```

### Specification files

A system specification may be written in one or several YAML files comprised
of the following sections:

 * distribution
 * playbook
 * image

#### distribution

The `distribution` section will be used to specify the primary source of the
packages that will make the end system. The following attributes are supported:

 * source: either `debian` or `ubuntu`
 * release: codename of the version to be used (e.g. `buster`)
 * architecture: one of `amd64`, `arm64` or `armhf`
 * uri: base location of the distribution packages

When multiple YAML files are parsed, the last parsed value will be used.

#### playbook

Ansible playbooks will be used to add packages to the system or configure them.
The `playbook` section is a list of `name` / `tasks` pairs:

```
playbook:
    - name: first playbook
      tasks:
          ...
    - name: second playbook
      tasks:
          ...
```

Playbooks may be given a priority between `0` and `999` with `0` being the
priority:

```
playbook:
    - name: first playbook but apply towards the end
      priority: 900
      tasks:
          ...
    - name: second playbook but apply early
      priority: 100
      tasks:
          ...
```

Frequently used tasks include:
 * `apt`
 * `debconf`

Additional packages may be installed as follows:

```
playbook:
    - name: install essential packages
      tasks:
          - name: base set
            apt:
                name:
                    - ssh
                    - vim
                state: present
```

and here is how the `locales` package may be configured:

```
playbook:
    -  name: configure locales to French
       tasks:
        - name: set default locale to fr_FR.UTF-8
          debconf:
              name: locales
              question: locales/default_environment_locale
              value: fr_FR.UTF-8
              vtype: select
```

#### image

Last but not least, the 'image' section defines the partition and volumes to be
created in the disk image. The following top-level attributes are supported:

 * `filename`
 * `partitions`
 * `size`
 * `table`
 * `volumes`

An `image` shall have at least one partition defined and an output `filename`
specified. The `size` of the disk `image` may be omitted and it will then be
estimated (as the sum of the various partition sizes plus some overhead). The
partition `table` may either be `gpt` or `msdos`.

#### partitions

Disk partitions are defined with the following attributes:

| Attribute | Required | Description                              |
| --------- |:--------:| ---------------------------------------- |
| label     | yes      | Name of the partition                    |
| flags     | no       | Partition flags (see below)              |
| group     | no       | Name of the LVM group to join            |
| size      | no       | Size of the partition                    |
| type      | no       | File-system type (e.g. `ext4`)           |
| where     | yes*     | Where to mount the partition file-system |

(*) Required unless the partition is a LVM physical volume

A partition may have the following flags:

| Flag     | Description                                          |
| -------- | ---------------------------------------------------- |
| boot     | system may boot from this partition                  |
| lvm      | partition will be used as a physical volume for LVM  |

When using a `msdos` partition table, the following flags are also available
(but are mutually exclusive):

 * primary
 * extended
 * logical

A `group` shall be defined for every single partition using the `lvm` flag and
may have one or several partitions attached to it. Groups implicitly defined
in the `partitions` section may be referenced by `volumes` (see below).

#### volumes

Logical volumes share many of the attributes defined above for `partitions` but
more specifically:

| Attribute | Required | Description                              |
| --------- |:--------:| ---------------------------------------- |
| label     | yes      | Name of the volume                       |
| group     | yes      | Name of the LVM group to join            |
| size      | no       | Size of the partition                    |
| type      | no       | File-system type (e.g. `ext4`)           |
| where     | yes      | Where to mount the volume file-system    |
