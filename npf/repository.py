import os
import tarfile
import urllib
from abc import ABCMeta
from pathlib import Path

import re

from npf import npf
from npf.build import Build
from .variable import is_numeric

import git

repo_variables=['name','branch','configure','url','method','parent','tags','make','version','clean','bin_folder','bin_name']

class Method(metaclass=ABCMeta):
    def __init__(self, repo):
        self.repo = repo

class MethodGit(Method):
    __gitrepo = None
    _fetch_done = False

    def gitrepo(self) -> git.Repo:
        if (self.__gitrepo):
            return self.__gitrepo
        else:
            return self.checkout()

    def get_last_versions(self,limit=100,branch=None):
        versions = []
        origin = self.gitrepo().remotes.origin
        if not self.repo.options.no_build and not self._fetch_done:
            print("Fetching last versions of %s..." % self.repo.reponame)
            origin.fetch()
            self._fetch_done = True

        if branch is None:
            branch = self.repo.branch

        for i, commit in enumerate(self.gitrepo().iter_commits('origin/' + branch)):
            versions.append(commit.hexsha[:7])
            if (len(versions) >= limit):
                break
        return versions

    def get_history(self, version, limit = 1):
        versions = []
        for commit in next(self.gitrepo().iter_commits(version)).iter_parents():
            versions.append(commit.hexsha[:7])
            if len(versions) == limit:
                break
        return versions

    def is_checkout_needed(self,version):
        gitrepo = self.gitrepo()
        if gitrepo.head.commit.hexsha[:7] != version:
            return True
        return False

    def checkout(self, branch=None) -> git.Repo:
        """
        Checkout the repo to its folder, fetch if it already exists
        :param branch: An optional branch
        :return:
        """
        if not branch:
            branch = self.repo.branch

        if os.path.exists(self.repo.get_build_path()):
            gitrepo = git.Repo(self.repo.get_build_path())
            o = gitrepo.remotes.origin
            if not self.repo.options.no_build:
                o.fetch()
        else:
            gitrepo = git.Repo.clone_from(self.repo.url, self.repo.get_build_path())
        if branch in gitrepo.remotes.origin.refs:
            c = gitrepo.remotes.origin.refs[branch]
        else:
            c = branch
            print("Checked out version %s" % c)
        gitrepo.head.reset(commit=c,index=True,working_tree=True)

        self.__gitrepo = gitrepo
        return gitrepo

class UnversionedMethod(Method,metaclass=ABCMeta):
    def __init__(self,repo):
        super().__init__(repo)
        if not repo.version:
            raise Exception("This method require a version")

    def get_last_versions(self,limit=None, branch=None):
        return [self.repo.version]


    def get_history(self, version, limit):
        return []



class MethodGet(UnversionedMethod):
    def checkout(self, branch=None):
        if branch is None:
            branch = self.repo.version
        url = self.repo.url.replace('$version',branch)
        if not Path(self.repo.get_build_path()).exists():
            os.makedirs(self.repo.get_build_path())
        filename, headers = urllib.request.urlretrieve(url,self.repo.get_build_path() + os.path.basename(url))
        t = tarfile.open(filename)
        t.extractall(self.repo.get_build_path())
        t.close()
        os.unlink(filename)
        return True

class MethodPackage(UnversionedMethod):
    def checkout(self, branch=None):
        pass

repo_methods = {'git' : MethodGit,'get':MethodGet,'package':MethodPackage}


class Repository:
    _repo_cache = {}
    configure = '--disable-linuxmodule'
    branch = 'master'
    make = 'make -j12'
    clean = 'make clean'
    bin_folder = 'bin'
    method = repo_methods['git']

    def __init__(self, repo, options):
        self.name = None
        self._current_build = None

        overwrite_name = repo.split(':')
        add_tags = overwrite_name[0].split('+')
        overwrite_branch = add_tags[0].split('/')

        self.reponame = overwrite_branch[0]
        self.tags=[]
        self.options = options
        self.bin_name=self.reponame #Wild guess that may work some times...

        repo_path = npf.find_local('repo/' + self.reponame + '.repo')

        f = open(repo_path, 'r')
        for line in f:
            line = line.strip()
            line = re.sub(r'(^|[ ])//.*$', '', line)
            if line.startswith("#"):
                continue
            if not line:
                continue
            s = line.split('=',1)
            val = s[1].strip()
            var = s[0].strip()
            append=False
            if var.endswith('+'):
                var = var[:-1]
                append=True

            if is_numeric(val) and var != 'branch':
                val = float(val)
                if val.is_integer():
                    val = int(val)
            if not var in repo_variables:
                raise Exception("Unknown variable %s" % var)
            elif var == "parent":
                parent = Repository(val,options)
                for attr in repo_variables:
                    if not hasattr(parent,attr):
                        continue
                    pval = getattr(parent,attr)
                    if attr == "method":
                        for m,c in repo_methods.items():
                            if c == type(pval):
                                method=m
                                break
                    else:
                        setattr(self,attr,pval)
            elif var == "method":
                val = val.lower()
                if not val in repo_methods:
                    raise Exception("Unknown method %s" % val)
                val = repo_methods[val]
            elif var == "tags":
                if append:
                    self.tags += val.split(',')
                else:
                    self.tags = val.split(',')
                continue

            if append:
                setattr(self,var,getattr(self,var) + " " +val)
            else:
                setattr(self,var,val)

        if len(add_tags) > 1:
            self.tags += add_tags[1].split(',')
            self._id = self.reponame + "-" + add_tags[1]
        else:
            self._id = self.reponame

        if len(overwrite_branch) > 1:
            self.branch = overwrite_branch[1]

        if len(overwrite_name) > 1:
            self.name = overwrite_name[1]

        self.method = self.method(self) #Instanciate the method
        self._build_path = os.path.dirname((options.build_folder if not options.build_folder is None else 'build/') + self.reponame + '/')

    def get_identifier(self):
        return self._id

    def get_reponame(self):
        return self.reponame

    def get_build_path(self):
        return self._build_path

    def get_bin_folder(self, version = None):
        if version is None:
            version = self.current_version()
        if version is None:
            bin_folder = self.bin_folder
        else:
            bin_folder = self.bin_folder.replace('$version', version)
        return self.get_build_path() + '/' + bin_folder + '/'

    def get_bin_path(self, version):
        if version is None:
            version = self.current_version()
        if version is None:
            bin_name = self.bin_name
        else:
            bin_name = self.bin_name.replace('$version', version)
        return self.get_bin_folder(version) + bin_name

    def get_last_build(self, history: int = 1, stop_at: Build = None, with_results = False) -> Build:
        versions = self.method.get_last_versions(100)

        last_build = None
        for i, version in enumerate(versions):
            if stop_at and version == stop_at.version:
                if i == 0:
                    return None
                break
            last_build = Build(self, version)
            if  not with_results or last_build.hasResults():
                if history <= 1:
                    break
                else:
                    history-=1
            if i > 100:
                last_build = None
                break
        return last_build

    def last_build_before(self, old_build) -> Build:
        return self.get_last_build(stop_at=old_build,history=-1)

    def get_old_results(self, last_graph:Build, num_old:int, testie):
        graphs_series = []
#Todo
        parents = self.method.gitrepo().iter_commits(last_graph.version)
        next(parents)  # The first commit is last_graph itself

        for i, commit in enumerate(parents):  # Get old results for graph
            g_build = Build(self, commit.hexsha[:7])
            if not g_build.hasResults(testie):
                continue
            g_all_results = g_build.load_results(testie)
            graphs_series.append((testie, g_build, g_all_results))
            if (i > 100 or len(graphs_series) == num_old):
                break
        return graphs_series

    def current_build(self):
        if self._current_build:
            return self._current_build
        return None

    def current_version(self):
        build = self.current_build()
        if build:
            return build.version
        return Build.get_current_version(self)

    @classmethod
    def get_instance(cls, dep, options):
        if dep in cls._repo_cache:
            return cls._repo_cache[dep]
        repo = Repository(dep, options)
        cls._repo_cache[dep] = repo
        return repo
