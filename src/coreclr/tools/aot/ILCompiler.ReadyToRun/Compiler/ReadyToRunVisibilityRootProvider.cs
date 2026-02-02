// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Collections.Generic;

using Internal.TypeSystem.Ecma;
using Internal.TypeSystem;
using Internal.JitInterface;
using System.Reflection.Metadata;
using System;

namespace ILCompiler
{
    /// <summary>
    /// Roots all possibly-visible methods in the input IL module.
    /// </summary>
    public class ReadyToRunVisibilityRootProvider : ICompilationRootProvider
    {
        private EcmaModule _module;
        private InstructionSetSupport _instructionSetSupport;

        public ReadyToRunVisibilityRootProvider(EcmaModule module)
        {
            _module = module;
            _instructionSetSupport = ((ReadyToRunCompilerContext)module.Context).InstructionSetSupport;
        }

        public void AddCompilationRoots(IRootingServiceProvider rootProvider)
        {
            int typeCount = 0;
            int rootedMethodCount = 0;
            int skippedGenericTypes = 0;
            int instantiatedGenericTypes = 0;
            
            Console.WriteLine($"[DIAG] ReadyToRunVisibilityRootProvider: Starting root discovery for {_module.Assembly.GetName().Name}");
            
            foreach (MetadataType type in _module.GetAllTypes())
            {
                typeCount++;
                MetadataType typeWithMethods = type;
                if (type.HasInstantiation)
                {
                    Console.WriteLine($"[DIAG]   Generic type DEFINED: {type} (params: {type.Instantiation.Length})");
                    typeWithMethods = ReadyToRunLibraryRootProvider.InstantiateIfPossible(type);
                    if (typeWithMethods == null)
                    {
                        Console.WriteLine($"[DIAG]     SKIPPED: Has valuetype constraint, cannot use __Canon");
                        skippedGenericTypes++;
                        continue;
                    }
                    Console.WriteLine($"[DIAG]     Instantiated as: {typeWithMethods}");
                    instantiatedGenericTypes++;
                }

                rootedMethodCount += RootMethods(typeWithMethods, "Library module method", rootProvider, ((EcmaAssembly)_module.Assembly).HasAssemblyCustomAttribute("System.Runtime.CompilerServices", "InternalsVisibleToAttribute"));
            }

            if (_module.EntryPoint is not null)
            {
                Console.WriteLine($"[DIAG]   Rooting entry point: {_module.EntryPoint}");
                rootProvider.AddCompilationRoot(_module.EntryPoint, rootMinimalDependencies: false, $"{_module.Assembly.GetName()} Main Method");
                rootedMethodCount++;
            }
            
            Console.WriteLine($"[DIAG] ReadyToRunVisibilityRootProvider for {_module.Assembly.GetName().Name}: scanned {typeCount} types, rooted {rootedMethodCount} methods");
            Console.WriteLine($"[DIAG]   Generic types: {instantiatedGenericTypes} instantiated with __Canon, {skippedGenericTypes} skipped (valuetype constraint)");
        }

        private int RootMethods(MetadataType type, string reason, IRootingServiceProvider rootProvider, bool anyInternalsVisibleTo)
        {
            int rootedCount = 0;
            int skippedVisibility = 0;
            int skippedGenericMethods = 0;
            int instantiatedGenericMethods = 0;
            
            MethodImplRecord[] methodImplRecords = GetAllMethodImplRecordsForType((EcmaType)type.GetTypeDefinition());
            foreach (MethodDesc method in type.GetAllMethods())
            {
                // Skip methods with no IL
                if (method.IsAbstract)
                    continue;

                if (method.IsInternalCall)
                    continue;

                // If the method is not visible outside the assembly, then do not root the method.
                // It will be rooted by any callers that require it and do not inline it.
                if (!method.IsStaticConstructor
                    && method.GetTypicalMethodDefinition() is EcmaMethod ecma
                    && !ecma.GetEffectiveVisibility().IsExposedOutsideOfThisAssembly(anyInternalsVisibleTo))
                {
                    // If a method itself is not visible outside the assembly, but it implements a method that is,
                    // we want to root it as it could be called from outside the assembly.
                    // Since instance method overriding does not always require a MethodImpl record (it can be omitted when both the name and signature match)
                    // we will also root any methods that are virtual and do not have any MethodImpl records as it is difficult to determine all methods a method
                    // overrides or implements and we don't need to be perfect here.
                    bool anyMethodImplRecordsForMethod = false;
                    bool implementsOrOverridesVisibleMethod = false;
                    foreach (var record in methodImplRecords)
                    {
                        if (record.Body == ecma)
                        {
                            anyMethodImplRecordsForMethod = true;
                            implementsOrOverridesVisibleMethod = record.Decl.GetTypicalMethodDefinition() is EcmaMethod decl
                                && decl.GetEffectiveVisibility().IsExposedOutsideOfThisAssembly(anyInternalsVisibleTo);
                            if (implementsOrOverridesVisibleMethod)
                            {
                                break;
                            }
                        }
                    }
                    if (anyMethodImplRecordsForMethod && !implementsOrOverridesVisibleMethod)
                    {
                        skippedVisibility++;
                        continue;
                    }
                    if (!anyMethodImplRecordsForMethod && !method.IsVirtual)
                    {
                        skippedVisibility++;
                        continue;
                    }
                }

                MethodDesc methodToRoot = method;
                if (method.HasInstantiation)
                {
                    Console.WriteLine($"[DIAG]     Generic method DEFINED: {type}.{method.Name.ToString()} (params: {method.Instantiation.Length})");
                    methodToRoot = ReadyToRunLibraryRootProvider.InstantiateIfPossible(method);

                    if (methodToRoot == null)
                    {
                        Console.WriteLine($"[DIAG]       SKIPPED: Has valuetype constraint, cannot use __Canon");
                        skippedGenericMethods++;
                        continue;
                    }
                    Console.WriteLine($"[DIAG]       Instantiated as: {methodToRoot}");
                    instantiatedGenericMethods++;
                }

                try
                {
                    if (!CorInfoImpl.ShouldSkipCompilation(_instructionSetSupport, method))
                    {
                        ReadyToRunLibraryRootProvider.CheckCanGenerateMethod(methodToRoot);
                        rootProvider.AddCompilationRoot(methodToRoot, rootMinimalDependencies: false, reason: reason);
                        rootedCount++;
                    }
                }
                catch (TypeSystemException ex)
                {
                    // Individual methods can fail to load types referenced in their signatures.
                    // Skip them in library mode since they're not going to be callable.
                    Console.WriteLine($"[DIAG]     Method {method.Name.ToString()} SKIPPED due to TypeSystemException: {ex.Message}");
                    continue;
                }
            }
            
            if (instantiatedGenericMethods > 0 || skippedGenericMethods > 0 || skippedVisibility > 0)
            {
                Console.WriteLine($"[DIAG]   Type {type}: rooted {rootedCount}, {instantiatedGenericMethods} generic methods instantiated, {skippedGenericMethods} skipped (constraint), {skippedVisibility} skipped (visibility)");
            }
            
            return rootedCount;
        }

        private MethodImplRecord[] GetAllMethodImplRecordsForType(EcmaType type)
        {
            ArrayBuilder<MethodImplRecord> records = default;
            MetadataReader metadataReader = type.MetadataReader;
            TypeDefinition definition = metadataReader.GetTypeDefinition(type.Handle);

            foreach (var methodImplHandle in definition.GetMethodImplementations())
            {
                MethodImplementation methodImpl = metadataReader.GetMethodImplementation(methodImplHandle);

                records.Add(new MethodImplRecord(
                    _module.GetMethod(methodImpl.MethodDeclaration),
                   _module.GetMethod(methodImpl.MethodBody)
                ));
            }
            return records.ToArray();
        }

        /// <summary>
        /// Determine if the visibility-based root provider should be used for the given module.
        /// </summary>
        /// <param name="module">The module</param>
        /// <returns><c>true</c> if the module should use the visibility-based root provider; otherwise, <c>false</c>.</returns>
        /// <remarks>
        /// We will use a visibility-based root provider for modules that are marked as trimmable.
        /// Trimmable assemblies are less likely to use private reflection, which the visibility-based root provider
        /// doesn't track well.
        /// </remarks>
        public static bool UseVisibilityBasedRootProvider(EcmaModule module)
        {
            EcmaAssembly assembly = (EcmaAssembly)module.Assembly;

            foreach (var assemblyMetadata in assembly.GetDecodedCustomAttributes("System.Reflection", "AssemblyMetadataAttribute"))
            {
                if ((string)assemblyMetadata.FixedArguments[0].Value == "IsTrimmable")
                {
                    return bool.TryParse((string)assemblyMetadata.FixedArguments[1].Value, out bool isTrimmable) && isTrimmable;
                }
            }
            return false;
        }
    }
}
