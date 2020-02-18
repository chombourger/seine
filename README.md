# seine: Singular Embedded Images Now Easy

## Introduction

seine is a command-line tool to build images for embedded systems based on the
Debian operating system. The system is specified in YAML (either as a single
file or several) and may include Ansible playbooks to install packages or
configure them.

The tool was designed to not require elevated privileges after its installation
(sudo isn't used or required, no bind mounts, etc.). The root file-system is
first assembled in a container (seine uses podman because it is daemon-less and
very similar to docker). It is then exported as a tarball and a Linux system
started under qemu/kvm to create the disk images including partitions and
logical volumes that were specified. Installation of the boot-loader also happens
there since it may require disks/partitions to be created.

## Getting started

### Installation

The easiest way to get started is to install the various packages to your Ubuntu
18.04 host using the following ppa:

```
sudo add-apt-repository ppa:chombourger/ppa
sudo apt-get update
sudo apt-get install -y podman python3-seine qemu-kvm
sudo usermod --add-subuids 10000-75535 --add-subgids 10000-75535 $USER
sudo adduser $USER kvm
```

You may then either use seine in place (use the `seine.py` script from the top
level directory of this source tree) or generate a binary package. To build
a sample image without installing `seine` on your system, use:

```
./seine.py build examples/pc-image/main.yaml
```

To produce a binary package, use the `dpkg-buildpackage` command as follows:

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

The specification may be broken down into smaller files to ease maintenance and
readability. This can be done using `requires` as shown below:

```
requires:
    - buster
    - amd64

distribution:
    - ...

playbook:
    - ...

image:
    - ...
```

For each module listed in the `requires` section, a corresponding file with
either the `.yml` or `.yaml` suffix shall be found in the folder of the yaml
file requiring them.

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
highest priority and `500` the default:

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
                state: present
                name:
                    - ssh
                    - vim
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

A minimal image that includes `apt` is used as starting point; `seine` uses
`apt` to install `ansible` and then executes the various playbooks according
to their `priority`. A different starting point may be specified with the
`baseline` keyword in the `playbook`:

```
playbook:
    - baseline: debian:buster
    - tasks:
      apt:
          ...
```

As `seine` uses `podman` behind the scene to create the root file-system in
a container, the `image` specified as `baseline` may be anything that can be
fetched from the `podman` or `docker` registries. The `image` shall however
have `apt` pre-installed (and `qemu-user-static` binaries for the host
architecture when building images for a foreign architecture).
 
#### image

Last but not least, the 'image' section defines the partition and volumes to be
created in the disk image. The following top-level attributes are supported:

 * `filename`
 * `bootlets`
 * `partitions`
 * `size`
 * `table`
 * `volumes`

An `image` shall have at least one partition defined and an output `filename`
specified. The `size` of the disk `image` may be omitted and it will then be
estimated (as the sum of the various partition sizes plus some overhead). The
partition `table` may either be `gpt` or `msdos`.

#### bootlets

Bootlets are binary firmware files placed at specific locations on the boot
media so they can be found by the hardware boot ROM. Examples include: u-boot,
Arm Trusted Firmware (ATF), etc.

The following attributes are supported:

| Attribute | Required | Description                              |
| --------- |:--------:| ---------------------------------------- |
| align     | no       | Expected alignment in Kilobytes (KiB)    |
| file      | yes      | Path to the binary to be copied (*)      |

(*) The specified will be copied from the image created by the `playbook`, a
    package shoud therefore install it.

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
