import { GetMyProfileData, UpdateMyProfileData, UpdateMyProfileVariables, SetUserProfileAccessData, SetUserProfileAccessVariables, ListMyBasketsData, CreateMyBasketData, CreateMyBasketVariables, UpdateMyBasketData, UpdateMyBasketVariables, DeleteMyBasketData, DeleteMyBasketVariables, GetMyRiskSettingsData, UpsertMyRiskSettingsData, UpsertMyRiskSettingsVariables } from '../';
import { UseDataConnectQueryResult, useDataConnectQueryOptions, UseDataConnectMutationResult, useDataConnectMutationOptions} from '@tanstack-query-firebase/react/data-connect';
import { UseQueryResult, UseMutationResult} from '@tanstack/react-query';
import { DataConnect } from 'firebase/data-connect';
import { FirebaseError } from 'firebase/app';


export function useGetMyProfile(options?: useDataConnectQueryOptions<GetMyProfileData>): UseDataConnectQueryResult<GetMyProfileData, undefined>;
export function useGetMyProfile(dc: DataConnect, options?: useDataConnectQueryOptions<GetMyProfileData>): UseDataConnectQueryResult<GetMyProfileData, undefined>;

export function useUpdateMyProfile(options?: useDataConnectMutationOptions<UpdateMyProfileData, FirebaseError, UpdateMyProfileVariables | void>): UseDataConnectMutationResult<UpdateMyProfileData, UpdateMyProfileVariables>;
export function useUpdateMyProfile(dc: DataConnect, options?: useDataConnectMutationOptions<UpdateMyProfileData, FirebaseError, UpdateMyProfileVariables | void>): UseDataConnectMutationResult<UpdateMyProfileData, UpdateMyProfileVariables>;

export function useSetUserProfileAccess(options?: useDataConnectMutationOptions<SetUserProfileAccessData, FirebaseError, SetUserProfileAccessVariables>): UseDataConnectMutationResult<SetUserProfileAccessData, SetUserProfileAccessVariables>;
export function useSetUserProfileAccess(dc: DataConnect, options?: useDataConnectMutationOptions<SetUserProfileAccessData, FirebaseError, SetUserProfileAccessVariables>): UseDataConnectMutationResult<SetUserProfileAccessData, SetUserProfileAccessVariables>;

export function useListMyBaskets(options?: useDataConnectQueryOptions<ListMyBasketsData>): UseDataConnectQueryResult<ListMyBasketsData, undefined>;
export function useListMyBaskets(dc: DataConnect, options?: useDataConnectQueryOptions<ListMyBasketsData>): UseDataConnectQueryResult<ListMyBasketsData, undefined>;

export function useCreateMyBasket(options?: useDataConnectMutationOptions<CreateMyBasketData, FirebaseError, CreateMyBasketVariables>): UseDataConnectMutationResult<CreateMyBasketData, CreateMyBasketVariables>;
export function useCreateMyBasket(dc: DataConnect, options?: useDataConnectMutationOptions<CreateMyBasketData, FirebaseError, CreateMyBasketVariables>): UseDataConnectMutationResult<CreateMyBasketData, CreateMyBasketVariables>;

export function useUpdateMyBasket(options?: useDataConnectMutationOptions<UpdateMyBasketData, FirebaseError, UpdateMyBasketVariables>): UseDataConnectMutationResult<UpdateMyBasketData, UpdateMyBasketVariables>;
export function useUpdateMyBasket(dc: DataConnect, options?: useDataConnectMutationOptions<UpdateMyBasketData, FirebaseError, UpdateMyBasketVariables>): UseDataConnectMutationResult<UpdateMyBasketData, UpdateMyBasketVariables>;

export function useDeleteMyBasket(options?: useDataConnectMutationOptions<DeleteMyBasketData, FirebaseError, DeleteMyBasketVariables>): UseDataConnectMutationResult<DeleteMyBasketData, DeleteMyBasketVariables>;
export function useDeleteMyBasket(dc: DataConnect, options?: useDataConnectMutationOptions<DeleteMyBasketData, FirebaseError, DeleteMyBasketVariables>): UseDataConnectMutationResult<DeleteMyBasketData, DeleteMyBasketVariables>;

export function useGetMyRiskSettings(options?: useDataConnectQueryOptions<GetMyRiskSettingsData>): UseDataConnectQueryResult<GetMyRiskSettingsData, undefined>;
export function useGetMyRiskSettings(dc: DataConnect, options?: useDataConnectQueryOptions<GetMyRiskSettingsData>): UseDataConnectQueryResult<GetMyRiskSettingsData, undefined>;

export function useUpsertMyRiskSettings(options?: useDataConnectMutationOptions<UpsertMyRiskSettingsData, FirebaseError, UpsertMyRiskSettingsVariables>): UseDataConnectMutationResult<UpsertMyRiskSettingsData, UpsertMyRiskSettingsVariables>;
export function useUpsertMyRiskSettings(dc: DataConnect, options?: useDataConnectMutationOptions<UpsertMyRiskSettingsData, FirebaseError, UpsertMyRiskSettingsVariables>): UseDataConnectMutationResult<UpsertMyRiskSettingsData, UpsertMyRiskSettingsVariables>;
